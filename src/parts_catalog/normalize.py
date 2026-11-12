from __future__ import annotations
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from .models import NormalizedPart, Quote, SupplierPart

# Version of the whole normalization rule set. Any change to a rule below
# must bump either the individual rule version or this set version, so ledger
# entries always name the exact logic that produced their output.
RULE_SET_VERSION = "normalize-rules-1"

_BRAND_ALIASES = {"acme inc.": "ACME", "acme": "ACME", "northstar ltd": "NORTHSTAR"}
_STATUS_ALIASES = {"in stock": "available", "available": "available", "out of stock": "unavailable", "backorder": "backorder"}


@dataclass(frozen=True)
class FieldTransform:
    """One field-level normalization step: rule id, the value seen in the
    raw input record, and the value written to the business entity."""

    rule_id: str
    field_name: str
    before: object
    after: object


@dataclass(frozen=True)
class AuditedPart:
    """A normalized supplier part together with the field-level evidence
    describing exactly which rules transformed which raw values."""

    part: SupplierPart
    transforms: tuple[FieldTransform, ...]
    raw_record: dict
    raw_index: int


def canonical_part_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def canonical_brand(value: str) -> str:
    cleaned = " ".join(value.strip().lower().split())
    return _BRAND_ALIASES.get(cleaned, cleaned.upper())


def normalize_part(raw: dict, raw_index: int | None = None) -> SupplierPart | AuditedPart:
    """Normalize one raw record.

    Backwards-compatible signature: with no ``raw_index`` (the historical
    call shape) it returns the :class:`SupplierPart` directly. Passing an
    explicit index returns the :class:`AuditedPart` carrying field-level
    transformation evidence; the pipeline always uses that form.
    """
    audited = normalize_part_audited(raw, raw_index if raw_index is not None else 0)
    if raw_index is None:
        return audited.part
    return audited


def normalize_part_audited(raw: dict, raw_index: int = 0) -> AuditedPart:
    """Normalize one raw record and emit a FieldTransform per persisted field.

    The transform list is the machine-readable answer to "which rule touched
    this value and what did it look like before/after".
    """
    transforms: list[FieldTransform] = []

    raw_price = raw.get("price")
    try:
        price = Decimal(str(raw_price)) if raw_price not in (None, "", "N/A") else None
    except InvalidOperation as exc:
        raise ValueError(f"invalid price for {raw.get('part_number')}") from exc
    transforms.append(
        FieldTransform("price.decimal-v1", "price", raw_price, None if price is None else str(price))
    )

    quantity = int(raw.get("package_quantity", 1))
    if quantity < 1:
        raise ValueError("package_quantity must be positive")
    transforms.append(
        FieldTransform("package_quantity.positive-int-v1", "package_quantity", raw.get("package_quantity", 1), quantity)
    )

    raw_supplier = str(raw["supplier"])
    supplier = raw_supplier.strip()
    transforms.append(FieldTransform("text.strip-v1", "supplier", raw_supplier, supplier))

    raw_part_number = str(raw["part_number"])
    part_number = canonical_part_number(raw_part_number)
    transforms.append(
        FieldTransform("part_number.canonical-alnum-v1", "part_number", raw_part_number, part_number)
    )

    raw_brand = str(raw["brand"])
    brand = canonical_brand(raw_brand)
    transforms.append(FieldTransform("brand.canonical-alias-v1", "brand", raw_brand, brand))

    raw_description = str(raw.get("description", ""))
    description = " ".join(raw_description.split())
    transforms.append(FieldTransform("text.whitespace-collapse-v1", "description", raw_description, description))

    raw_unit = str(raw.get("unit", "EA"))
    unit = raw_unit.upper()
    transforms.append(FieldTransform("text.upper-default-v1", "unit", raw_unit, unit))

    raw_lead = raw.get("lead_time_days")
    lead_time_days = int(raw_lead) if raw_lead is not None else None
    transforms.append(FieldTransform("int.optional-v1", "lead_time_days", raw_lead, lead_time_days))

    raw_status = str(raw.get("stock_status", "unknown"))
    stock_status = _STATUS_ALIASES.get(raw_status.lower(), "unknown")
    transforms.append(FieldTransform("status.alias-map-v1", "stock_status", raw_status, stock_status))

    part = SupplierPart(
        supplier=supplier,
        part_number=part_number,
        brand=brand,
        description=description,
        unit=unit,
        package_quantity=quantity,
        lead_time_days=lead_time_days,
        stock_status=stock_status,
        price=price,
    )
    return AuditedPart(part=part, transforms=tuple(transforms), raw_record=dict(raw), raw_index=raw_index)


def to_catalog_part(part: SupplierPart) -> NormalizedPart:
    return NormalizedPart(part.part_number, part.brand, part.description, part.unit, part.package_quantity)


def to_quote(part: SupplierPart, currency: str, source_ref: str) -> Quote:
    return Quote(part.part_number, part.supplier, currency.upper(), part.price, part.lead_time_days, part.stock_status, source_ref)


def quote_transforms(part: SupplierPart, currency: str, source_ref: str) -> tuple[FieldTransform, ...]:
    """Field-level rules applied when deriving the quote entity from a
    normalized supplier part."""
    return (
        FieldTransform("quote.identity-v1", "canonical_number", part.part_number, part.part_number),
        FieldTransform("quote.identity-v1", "supplier", part.supplier, part.supplier),
        FieldTransform("text.upper-v1", "currency", currency, currency.upper()),
        FieldTransform("quote.identity-v1", "price", None if part.price is None else str(part.price),
                       None if part.price is None else str(part.price)),
        FieldTransform("quote.identity-v1", "lead_time_days", part.lead_time_days, part.lead_time_days),
        FieldTransform("quote.identity-v1", "stock_status", part.stock_status, part.stock_status),
        FieldTransform("quote.source-ref-v1", "source_ref", source_ref, source_ref),
    )
