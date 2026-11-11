from __future__ import annotations

from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore


class PartsPipeline:
    """Imports one supplier payload as a single unit of work.

    The whole payload is normalized and written inside one import batch:
    either every record is published together, or — on any failure, including
    a crash of the nightly job — the previously committed data is left
    untouched and the run can simply be retried.
    """

    def __init__(self, source: PartSource, store: PartsStore) -> None:
        self.source = source
        self.store = store

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> int:
        with self.store.import_batch() as batch:
            count = 0
            for raw in self.source.read():
                supplier_part = normalize_part(raw)
                batch.save_part(to_catalog_part(supplier_part))
                batch.save_quote(to_quote(supplier_part, currency, source_ref))
                count += 1
        return count
