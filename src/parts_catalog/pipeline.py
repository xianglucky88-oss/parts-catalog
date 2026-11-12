from __future__ import annotations
from .merge import facts_from_record
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore

MANUAL_ORIGIN = "manual"


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore) -> None:
        self.source = source
        self.store = store

    def run(self, currency: str = "USD", source_ref: str = "fixture", origin: str = "feed") -> int:
        count = 0
        for raw in self.source.read():
            supplier_part = normalize_part(raw)
            # A record may carry its own origin/note (e.g. a master-data
            # sign-off inside a mixed file); otherwise the run-level origin
            # applies. Unknown origins are rejected by facts_from_record.
            record_origin = str(raw.get("origin", origin)).strip().lower()
            raw_note = raw.get("note")
            note = str(raw_note) if raw_note is not None else None
            catalog_part = to_catalog_part(supplier_part)
            facts = facts_from_record(catalog_part, raw, record_origin, source_ref, note)
            self.store.merge_part(catalog_part, facts)
            # Quotes are independent supplier facts: they are stored
            # unconditionally for supplier feeds, regardless of how the
            # catalog fields were adjudicated. Manual sign-off records are
            # master data, not supplier facts, so they never create quotes.
            if record_origin != MANUAL_ORIGIN:
                self.store.save_quote(to_quote(supplier_part, currency, source_ref))
            count += 1
        self.store.commit()
        return count
