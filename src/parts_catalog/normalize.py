from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
from .models import NormalizedPart, Quote, SupplierPart

_BRAND_ALIASES = {"acme inc.": "ACME", "acme": "ACME", "northstar ltd": "NORTHSTAR"}
_STATUS_ALIASES = {"in stock": "available", "available": "available", "out of stock": "unavailable", "backorder": "backorder"}


def canonical_part_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def canonical_brand(value: str) -> str:
    cleaned = " ".join(value.strip().lower().split())
    return _BRAND_ALIASES.get(cleaned, cleaned.upper())


def normalize_part(raw: dict) -> SupplierPart:
    raw_price = raw.get("price")
    try:
        price = Decimal(str(raw_price)) if raw_price not in (None, "", "N/A") else None
    except InvalidOperation as exc:
        raise ValueError(f"invalid price for {raw.get('part_number')}") from exc
    quantity = int(raw.get("package_quantity", 1))
    if quantity < 1:
        raise ValueError("package_quantity must be positive")
    return SupplierPart(
        supplier=str(raw["supplier"]).strip(),
        part_number=canonical_part_number(str(raw["part_number"])),
        brand=canonical_brand(str(raw["brand"])),
        description=" ".join(str(raw.get("description", "")).split()),
        unit=str(raw.get("unit", "EA")).upper(),
        package_quantity=quantity,
        lead_time_days=int(raw["lead_time_days"]) if raw.get("lead_time_days") is not None else None,
        stock_status=_STATUS_ALIASES.get(str(raw.get("stock_status", "unknown")).lower(), "unknown"),
        price=price,
    )


def to_catalog_part(part: SupplierPart) -> NormalizedPart:
    return NormalizedPart(part.part_number, part.brand, part.description, part.unit, part.package_quantity)


def to_quote(part: SupplierPart, currency: str, source_ref: str) -> Quote:
    return Quote(part.part_number, part.supplier, currency.upper(), part.price, part.lead_time_days, part.stock_status, source_ref)
