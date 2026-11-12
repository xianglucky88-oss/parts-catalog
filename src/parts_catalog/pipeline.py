from __future__ import annotations
from dataclasses import dataclass
from typing import Callable

from .evidence import (
    RUN_COMMITTED,
    input_digest,
    make_entry,
    make_run_id,
)
from .normalize import (
    RULE_SET_VERSION,
    normalize_part_audited,
    quote_transforms,
    to_catalog_part,
    to_quote,
)
from .sources import PartSource
from .storage import PartsStore, quote_entity_key, snapshot_part, snapshot_quote


@dataclass(frozen=True)
class RunResult:
    run_id: str
    input_hash: str
    replayed: bool
    record_count: int
    entries_written: int


# A fault hook receives the store right before the run transaction is
# committed. Raising there must leave zero business facts and zero evidence
# behind; it is the seam used to prove crash/rollback semantics in tests.
FaultHook = Callable[[PartsStore], None]


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore, fault_hook: FaultHook | None = None) -> None:
        self.source = source
        self.store = store
        self.fault_hook = fault_hook

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> RunResult:
        raw_records = list(self.source.read())
        in_hash = input_digest(raw_records)
        run_id = make_run_id(in_hash, source_ref, currency, RULE_SET_VERSION)

        # Idempotency: the run identity is derived solely from the input
        # content and run parameters. An identical replay is recognized before
        # any write and adds neither a second business fact nor a second
        # piece of evidence.
        if self.store.run_exists(run_id):
            return RunResult(run_id, in_hash, replayed=True, record_count=len(raw_records), entries_written=0)

        self.store.begin_run()
        try:
            seq, prev_hash = self.store.chain_head()
            entries_written = 0

            for raw_index, raw in enumerate(raw_records):
                audited = normalize_part_audited(raw, raw_index=raw_index)
                part = audited.part
                catalog_part = to_catalog_part(part)
                quote = to_quote(part, currency, source_ref)

                # ---- catalog part: capture pre-image, write, evidence ----
                part_before = self.store.get_part(catalog_part.canonical_number)
                self.store.save_part(catalog_part)
                part_after = snapshot_part(catalog_part)
                part_transforms = [
                    {"rule_id": t.rule_id, "field_name": t.field_name, "before": t.before, "after": t.after}
                    for t in audited.transforms
                ]
                seq += 1
                part_entry = make_entry(
                    seq=seq,
                    run_id=run_id,
                    input_hash=in_hash,
                    raw_index=raw_index,
                    raw_record=audited.raw_record,
                    entity_type="part",
                    entity_key=catalog_part.canonical_number,
                    rule_set_version=RULE_SET_VERSION,
                    rule_ids=sorted({t.rule_id for t in audited.transforms}),
                    transforms=part_transforms,
                    before_snapshot=part_before,
                    after_snapshot=part_after,
                    prev_hash=prev_hash,
                )
                self.store.append_entry(part_entry)
                prev_hash = part_entry.entry_hash
                entries_written += 1

                # ---- quote: same transaction, its own evidence entry ------
                q_key = quote_entity_key(quote.canonical_number, quote.supplier, quote.source_ref)
                quote_before = self.store.get_quote(quote.canonical_number, quote.supplier, quote.source_ref)
                self.store.save_quote(quote)
                quote_after = snapshot_quote(quote)
                q_transforms_raw = quote_transforms(part, currency, source_ref)
                q_transforms = [
                    {"rule_id": t.rule_id, "field_name": t.field_name, "before": t.before, "after": t.after}
                    for t in q_transforms_raw
                ]
                seq += 1
                quote_entry = make_entry(
                    seq=seq,
                    run_id=run_id,
                    input_hash=in_hash,
                    raw_index=raw_index,
                    raw_record=audited.raw_record,
                    entity_type="quote",
                    entity_key=q_key,
                    rule_set_version=RULE_SET_VERSION,
                    rule_ids=sorted({t.rule_id for t in q_transforms_raw}),
                    transforms=q_transforms,
                    before_snapshot=quote_before,
                    after_snapshot=quote_after,
                    prev_hash=prev_hash,
                )
                self.store.append_entry(quote_entry)
                prev_hash = quote_entry.entry_hash
                entries_written += 1

            # The run row is written inside the same transaction: it can only
            # become visible once every fact and evidence entry is durable.
            self.store.insert_run(
                run_id=run_id,
                input_hash=in_hash,
                source_ref=source_ref,
                currency=currency,
                rule_set_version=RULE_SET_VERSION,
                record_count=len(raw_records),
                status=RUN_COMMITTED,
            )

            if self.fault_hook is not None:
                self.fault_hook(self.store)

            self.store.commit()
        except BaseException:
            # An interrupted write must never masquerade as a completed run:
            # discard the whole transaction, including the run marker.
            self.store.rollback()
            raise

        return RunResult(
            run_id,
            in_hash,
            replayed=False,
            record_count=len(raw_records),
            entries_written=entries_written,
        )
