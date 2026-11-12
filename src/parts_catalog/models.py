from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class SupplierPart:
    supplier: str
    part_number: str
    brand: str
    description: str
    unit: str
    package_quantity: int | None
    lead_time_days: int | None
    stock_status: str
    price: Decimal | None


@dataclass(frozen=True)
class NormalizedPart:
    canonical_number: str
    brand: str
    description: str
    unit: str
    package_quantity: int | None


@dataclass(frozen=True)
class Quote:
    canonical_number: str
    supplier: str
    currency: str
    price: Decimal | None
    lead_time_days: int | None
    stock_status: str
    source_ref: str
    # Packaging dimension of a validated quote.  A missing package_quantity is
    # an explicit unknown: it must never be silently defaulted to one piece.
    unit: str = "EA"
    package_quantity: int | None = None


@dataclass(frozen=True)
class PipelineResult:
    processed: int
    exact_unit_costs: int
    incomparable: int
