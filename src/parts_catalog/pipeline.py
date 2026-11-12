from __future__ import annotations

from .admission import AdmissionDecision
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore) -> None:
        self.source = source
        self.store = store

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> dict:
        """采集并持久化全部报价；每条报价都必须经过准入闸。

        被拒记录不会被丢弃：原始值与拒绝理由由存储层写入留痕表，
        但绝不会进入 eligible_quotes 比较集合。
        """
        decisions: list[AdmissionDecision] = []
        processed = 0
        for raw in self.source.read():
            supplier_part = normalize_part(raw)
            # 允许逐行覆盖货币；未提供时使用全局参数。原始文本交准入闸规范化。
            row_currency = raw.get("currency", currency)
            quote = to_quote(supplier_part, row_currency, source_ref)
            # 型号行、报价行与准入留痕在同一事务内落库。
            with self.store.connection:
                self.store.save_part(to_catalog_part(supplier_part))
                decisions.append(self.store.admit_and_store(quote))
            processed += 1
        # admit_and_store 已在事务内逐条提交；这里无需再 commit。
        admitted = sum(1 for decision in decisions if decision.admitted)
        return {
            "processed": processed,
            "admitted": admitted,
            "rejected": processed - admitted,
            "decisions": decisions,
        }
