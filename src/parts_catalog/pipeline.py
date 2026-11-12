from __future__ import annotations
from .entity_registry import SupplierRegistry
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore, registry: SupplierRegistry | None = None) -> None:
        self.source = source
        self.store = store
        self.registry = registry

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> int:
        count = 0
        for raw in self.source.read():
            supplier_part = normalize_part(raw)
            supplier_entity_id = None
            if self.registry is not None:
                observation = self.registry.observe(supplier_part.supplier, source_ref)
                decision = self.registry.current_decision(observation.observation_id)
                if decision and decision.state == "resolved":
                    supplier_entity_id = decision.entity_id
            self.store.save_part(to_catalog_part(supplier_part))
            self.store.save_quote(to_quote(supplier_part, currency, source_ref, supplier_entity_id))
            count += 1
        self.store.commit()
        return count
