"""Idempotent batch ingestion for collector envelopes.

Outcomes are deliberately explicit so the data pipeline can rely on them:
- ``imported``:  every result in the batch was imported;
- ``empty``:     the batch carried zero results (a valid, recorded state);
- ``partial``:   some results imported, some failed (row-level failures are
                 captured, never silently dropped, and never abort the batch);
- ``failed``:    results were present but none could be imported;
- ``duplicate``: the batch_id was already ingested; nothing is re-imported.

Every processed batch is recorded in the ``ingestion_batches`` ledger, which
is what makes duplicate detection and audit possible across runs.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .handoff import BatchEnvelope
from .models import BatchRecord
from .normalize import normalize_part, to_catalog_part, to_quote
from .storage import PartsStore

OUTCOMES = ("imported", "empty", "partial", "failed", "duplicate")


@dataclass(frozen=True)
class ResultFailure:
    index: int
    code: str
    message: str


@dataclass(frozen=True)
class IngestReport:
    batch_id: str
    supplier: str
    outcome: str
    received: int
    imported: int
    failed: int
    errors: tuple[ResultFailure, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "supplier": self.supplier,
            "outcome": self.outcome,
            "received": self.received,
            "imported": self.imported,
            "failed": self.failed,
            "errors": [{"index": e.index, "code": e.code, "message": e.message} for e in self.errors],
        }


class BatchIngestor:
    def __init__(self, store: PartsStore) -> None:
        self.store = store

    def ingest(self, envelope: BatchEnvelope, currency: str = "USD") -> IngestReport:
        if self.store.batch_seen(envelope.batch_id):
            return IngestReport(
                batch_id=envelope.batch_id,
                supplier=envelope.supplier,
                outcome="duplicate",
                received=len(envelope.results),
                imported=0,
                failed=0,
            )
        imported = 0
        failures: list[ResultFailure] = []
        for index, result in enumerate(envelope.results):
            if result.status == "error":
                failures.append(ResultFailure(index, result.error.code, result.error.message))
                continue
            try:
                part = normalize_part(result.part)
                self.store.save_part(to_catalog_part(part))
                self.store.save_quote(to_quote(part, currency, source_ref=envelope.batch_id))
                imported += 1
            except Exception as exc:  # one malformed row must not abort the batch
                failures.append(ResultFailure(index, "normalize_failed", str(exc)))
        received = len(envelope.results)
        failed = len(failures)
        if received == 0:
            outcome = "empty"
        elif failed == 0:
            outcome = "imported"
        elif imported == 0:
            outcome = "failed"
        else:
            outcome = "partial"
        self.store.record_batch(
            BatchRecord(
                batch_id=envelope.batch_id,
                supplier=envelope.supplier,
                queried_at=envelope.queried_at,
                query_part_number=envelope.query_part_number,
                status=outcome,
                received_count=received,
                imported_count=imported,
                failed_count=failed,
                source_ref=envelope.batch_id,
                ingested_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.store.commit()
        return IngestReport(
            batch_id=envelope.batch_id,
            supplier=envelope.supplier,
            outcome=outcome,
            received=received,
            imported=imported,
            failed=failed,
            errors=tuple(failures),
        )
