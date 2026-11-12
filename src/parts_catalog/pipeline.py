from __future__ import annotations

from .normalize import to_catalog_part, to_quote
from .quality import ACCEPTED, QUARANTINED, ImportReport, QualityPolicy, REJECTED
from .sources import PartSource
from .storage import PartsStore


class PartsPipeline:
    """Import entry with a versioned business-quality gate.

    Records that pass the policy (``accepted``) keep the original import
    semantics: a catalog part and a quote are upserted.  ``quarantined`` and
    ``rejected`` records never touch the business tables; their field-level
    reasons are persisted as quality decisions instead.  Per-supplier quality
    profiles are stored for every run.
    """

    def __init__(
        self,
        source: PartSource,
        store: PartsStore,
        policy: QualityPolicy | None = None,
    ) -> None:
        self.source = source
        self.store = store
        self.policy = policy or QualityPolicy()

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> int:
        """Run the import and return the number of records evaluated."""
        return len(self.run_with_report(currency, source_ref).decisions)

    def run_with_report(self, currency: str = "USD", source_ref: str = "fixture") -> ImportReport:
        report = self.policy.evaluate_batch(self.source.read())
        for decision in report.decisions:
            if decision.verdict == ACCEPTED and decision.part is not None:
                # Unchanged semantics for conforming records: catalog + quote.
                self.store.save_part(to_catalog_part(decision.part))
                self.store.save_quote(to_quote(decision.part, currency, source_ref))
            elif decision.verdict in (REJECTED, QUARANTINED):
                self.store.save_quality_decision(decision, source_ref)
        for profile in report.profiles:
            self.store.save_quality_profile(profile, source_ref)
        self.store.commit()
        return report
