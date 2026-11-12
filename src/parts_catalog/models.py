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
    # 可解析时为 Decimal；无法解析的历史文本原样保留，由准入闸拒绝并留痕。
    price: Decimal | str | None


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
    # 与 SupplierPart.price 一致：保留原始形态，准入闸负责精确性与有限性校验。
    price: Decimal | str | None
    lead_time_days: int | None
    stock_status: str
    source_ref: str
