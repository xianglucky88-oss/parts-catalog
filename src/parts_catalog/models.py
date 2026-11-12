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
class ScopeEntry:
    """One query pinned when a collection run starts: ask this supplier about
    this canonical part number. A run may only ever affect these cells."""

    supplier: str
    canonical_number: str


@dataclass(frozen=True)
class RunInfo:
    """Lifecycle state of a collection run.

    status: "collecting" (deliveries accepted, nothing visible yet),
    "published" (scope fully covered and atomically published; immutable),
    "failed" (interrupted; staged deliveries are kept for a safe retry).
    """

    run_id: str
    source_ref: str
    status: str
    expected_queries: int
    staged_queries: int
    started_at: str
    finished_at: str | None
    valid_until: str | None


@dataclass(frozen=True)
class VisibleQuote:
    """One row of the procurement-visible view.

    visibility is "current" while the publication is inside its validity
    window and "expired" afterwards. Expired rows stay explicitly listed so a
    supplier is never silently presented as having no quote.
    """

    supplier: str
    canonical_number: str
    currency: str
    price: Decimal | None
    lead_time_days: int | None
    stock_status: str
    run_id: str
    source_ref: str
    published_at: str
    valid_until: str | None
    visibility: str
