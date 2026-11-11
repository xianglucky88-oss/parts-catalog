from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SupplierPart:
    supplier: str
    part_number: str
    brand: str
    description: str
    unit: str
    package_quantity: int
    lead_time_days: int | None
    stock_status: str
    price: Decimal | None


@dataclass(frozen=True)
class NormalizedPart:
    canonical_number: str
    brand: str
    description: str
    unit: str
    package_quantity: int


@dataclass(frozen=True)
class Quote:
    canonical_number: str
    supplier: str
    currency: str
    price: Decimal | None
    lead_time_days: int | None
    stock_status: str
    source_ref: str


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    supplier: str
    queried_at: str
    query_part_number: str
    status: str
    received_count: int
    imported_count: int
    failed_count: int
    source_ref: str
    ingested_at: str
