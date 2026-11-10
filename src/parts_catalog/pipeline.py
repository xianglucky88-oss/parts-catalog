from __future__ import annotations
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore) -> None:
        self.source = source
        self.store = store

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> int:
        count = 0
        for raw in self.source.read():
            supplier_part = normalize_part(raw)
            self.store.save_part(to_catalog_part(supplier_part))
            self.store.save_quote(to_quote(supplier_part, currency, source_ref))
            count += 1
        self.store.commit()
        return count
