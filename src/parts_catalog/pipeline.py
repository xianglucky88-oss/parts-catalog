from __future__ import annotations
from .models import PipelineResult
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore
from .unit_cost import UnitCostStatus, evaluate_unit_cost


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore) -> None:
        self.source = source
        self.store = store

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> PipelineResult:
        processed = 0
        exact = 0
        incomparable = 0
        for raw in self.source.read():
            supplier_part = normalize_part(raw)
            quote = to_quote(supplier_part, currency, source_ref)
            self.store.save_part(to_catalog_part(supplier_part))
            self.store.save_quote(quote)
            result = evaluate_unit_cost(quote)
            self.store.save_unit_cost(result, source_ref)
            processed += 1
            if result.status is UnitCostStatus.EXACT:
                exact += 1
            else:
                incomparable += 1
        self.store.commit()
        return PipelineResult(processed=processed, exact_unit_costs=exact, incomparable=incomparable)
