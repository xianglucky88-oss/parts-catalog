from __future__ import annotations
from typing import Protocol
from .models import RawPartEnvelope, SupplierPart
from .normalize import normalize_part


class SourceContractError(Exception):
    """A source record was rejected at the adapter boundary.

    Always carries provenance (origin, locator, supplier, contract version)
    so a collection failure can be attributed to one concrete record.
    """

    def __init__(self, reason: str, *, supplier: str | None = None, contract_version: str | None = None,
                 origin: str | None = None, locator: str | None = None) -> None:
        self.reason = reason
        self.supplier = supplier
        self.contract_version = contract_version
        self.origin = origin
        self.locator = locator
        super().__init__(reason)

    def __str__(self) -> str:
        where = f"{self.origin or '?'}#{self.locator or '?'}"
        return (f"[{where}] supplier={self.supplier or '?'} "
                f"contract={self.contract_version or '<default>'}: {self.reason}")


class UnknownContractError(SourceContractError):
    """No adapter is registered for the record's (supplier, contract_version)."""


class RecordContractViolationError(SourceContractError):
    """The record violates its supplier contract or the unified SupplierPart contract."""


class SourceBatchRejectedError(Exception):
    """The batch contained unknown contracts or violating records.

    Raised before any storage write, so a rejected batch leaves the store untouched.
    """

    def __init__(self, rejections: list[SourceContractError]) -> None:
        self.rejections = tuple(rejections)
        details = "\n".join(f"  - {rejection}" for rejection in self.rejections)
        super().__init__(
            f"{len(self.rejections)} source record(s) rejected before any storage write:\n{details}")


class SupplierAdapter(Protocol):
    """Transforms one supplier's raw record shape into the unified SupplierPart."""
    supplier: str
    contract_version: str

    def adapt(self, envelope: RawPartEnvelope) -> SupplierPart: ...


def _supplier_key(supplier: str | None) -> str:
    return " ".join(str(supplier or "").strip().lower().split())


class AdapterRegistry:
    """Selects the adapter for a (supplier, contract_version) pair.

    Records without an explicit contract version fall back to the supplier's
    default adapter; unknown suppliers and explicit unknown versions are rejected.
    """

    def __init__(self) -> None:
        self._by_contract: dict[tuple[str, str], SupplierAdapter] = {}
        self._defaults: dict[str, SupplierAdapter] = {}

    def register(self, adapter: SupplierAdapter, *, default: bool = False) -> None:
        key = (_supplier_key(adapter.supplier), str(adapter.contract_version).strip())
        if key in self._by_contract:
            raise ValueError(f"adapter already registered for supplier {key[0]!r} contract {key[1]!r}")
        self._by_contract[key] = adapter
        if default:
            if key[0] in self._defaults:
                raise ValueError(f"default adapter already registered for supplier {key[0]!r}")
            self._defaults[key[0]] = adapter

    def resolve(self, supplier: str | None, contract_version: str | None, *,
                origin: str | None = None, locator: str | None = None) -> SupplierAdapter:
        key = _supplier_key(supplier)
        if contract_version is None:
            adapter = self._defaults.get(key)
            if adapter is None:
                raise UnknownContractError(
                    f"no default adapter for supplier {supplier!r}",
                    supplier=supplier, origin=origin, locator=locator)
            return adapter
        version = str(contract_version).strip()
        adapter = self._by_contract.get((key, version))
        if adapter is None:
            raise UnknownContractError(
                f"no adapter for supplier {supplier!r} contract {version!r}",
                supplier=supplier, contract_version=version, origin=origin, locator=locator)
        return adapter


def validate_supplier_part(part: SupplierPart, envelope: RawPartEnvelope) -> None:
    """Unified-contract invariants every adapter output must satisfy before storage."""
    problems = []
    if not part.supplier.strip():
        problems.append("supplier is empty")
    if not part.part_number:
        problems.append("part_number is empty after canonicalization")
    if not part.brand:
        problems.append("brand is empty")
    if not part.unit.strip():
        problems.append("unit is empty")
    if part.package_quantity < 1:
        problems.append("package_quantity must be positive")
    if problems:
        raise RecordContractViolationError(
            "; ".join(problems),
            supplier=envelope.supplier, contract_version=envelope.contract_version,
            origin=envelope.origin, locator=envelope.locator)


def _violation(adapter: SupplierAdapter, envelope: RawPartEnvelope, exc: Exception) -> RecordContractViolationError:
    if isinstance(exc, KeyError):
        detail = f"missing required field {exc}"
    else:
        detail = str(exc)
    return RecordContractViolationError(
        f"record violates {adapter.supplier}/{adapter.contract_version}: {detail}",
        supplier=envelope.supplier, contract_version=adapter.contract_version,
        origin=envelope.origin, locator=envelope.locator)


def _require_mapping(record: dict, key: str) -> dict:
    value = record.get(key)
    if not isinstance(value, dict):
        raise KeyError(key)
    return value


def _require_value(section: dict, key: str, path: str):
    value = section.get(key)
    if value is None:
        raise KeyError(path)
    return value


def _optional_mapping(record: dict, key: str) -> dict:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


class NorthstarV1Adapter:
    """Northstar legacy flat record shape (implicit contract 1.0)."""

    supplier = "northstar"
    contract_version = "1.0"

    def adapt(self, envelope: RawPartEnvelope) -> SupplierPart:
        try:
            return normalize_part(envelope.record)
        except (KeyError, ValueError) as exc:
            raise _violation(self, envelope, exc) from exc


class VertexV21Adapter:
    """Vertex contract 2.1: part number, brand and stock live in nested objects."""

    supplier = "vertex"
    contract_version = "2.1"

    def adapt(self, envelope: RawPartEnvelope) -> SupplierPart:
        record = envelope.record
        try:
            sku = _require_mapping(record, "sku")
            pack = _require_mapping(record, "pack")
            availability = _require_mapping(record, "availability")
            naming = _optional_mapping(record, "naming")
            pricing = _optional_mapping(record, "pricing")
            flat = {
                "supplier": envelope.supplier or "Vertex",
                "part_number": _require_value(sku, "code", "sku.code"),
                "brand": _require_value(sku, "maker", "sku.maker"),
                "description": naming.get("title", ""),
                "unit": pack.get("uom", "EA"),
                "package_quantity": _require_value(pack, "qty", "pack.qty"),
                "lead_time_days": availability.get("lead_days"),
                "stock_status": str(availability.get("state", "unknown")).replace("_", " "),
                "price": pricing.get("amount"),
            }
            return normalize_part(flat)
        except (KeyError, ValueError) as exc:
            raise _violation(self, envelope, exc) from exc


def default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(NorthstarV1Adapter(), default=True)
    registry.register(VertexV21Adapter())
    return registry
