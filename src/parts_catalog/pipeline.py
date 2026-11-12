from __future__ import annotations
from dataclasses import dataclass
from .errors import DeliveryError, IncompleteRunError, RunAlreadyPublishedError
from .models import RunInfo, ScopeEntry
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore


@dataclass(frozen=True)
class RunOutcome:
    """Result of one collect() cycle.

    status: "published" (the visible view was replaced for the run's scope),
    "collecting" (deliveries staged, queries still pending — the previous
    complete publication stays visible), or "already_published" (a repeated
    run of an already-published round; nothing changed).
    """

    run_id: str
    status: str
    delivered: int
    pending: tuple
    source_ref: str


class PartsPipeline:
    def __init__(self, source: PartSource, store: PartsStore) -> None:
        self.source = source
        self.store = store

    def plan_scope(self) -> list[ScopeEntry]:
        """Derive the query scope from the source payload itself, used when no
        separate manifest pins the run. Duplicate cells collapse."""
        entries: list[ScopeEntry] = []
        seen = set()
        problems = []
        for index, raw in enumerate(self.source.read()):
            try:
                part = normalize_part(raw)
            except Exception as exc:  # record-level validation failure
                problems.append(f"record {index}: {exc}")
                continue
            key = (part.supplier, part.part_number)
            if key not in seen:
                seen.add(key)
                entries.append(ScopeEntry(part.supplier, part.part_number))
        if problems:
            raise DeliveryError(problems)
        return entries

    def begin_run(self, run_id: str, source_ref: str, scope, valid_until=None) -> RunInfo:
        return self.store.begin_run(run_id, source_ref, list(scope), valid_until=valid_until)

    def deliver(self, run_id: str, currency: str = "USD") -> int:
        """Validate and stage one delivery batch.

        The whole batch is normalized first: a single faulty record rejects
        the batch (DeliveryError) and nothing is staged, so the run stays in
        a clean, retryable state.
        """
        info = self.store.run_info(run_id)
        parts, quotes, problems = [], [], []
        for index, raw in enumerate(self.source.read()):
            try:
                part = normalize_part(raw)
            except Exception as exc:  # record-level validation failure
                problems.append(f"record {index}: {exc}")
                continue
            parts.append(to_catalog_part(part))
            quotes.append(to_quote(part, currency, info.source_ref))
        if problems:
            raise DeliveryError(problems)
        return self.store.stage_delivery(run_id, parts, quotes)

    def collect(
        self,
        run_id: str,
        currency: str = "USD",
        source_ref: str = "fixture",
        scope=None,
        valid_until=None,
    ) -> RunOutcome:
        """One collection cycle: pin the scope, stage the delivery, and
        publish only if the pinned scope is now fully answered.

        When the scope is not covered, the run stays "collecting" and the
        previous complete publication remains the visible view.
        """
        pinned = list(scope) if scope is not None else self.plan_scope()
        try:
            self.store.begin_run(run_id, source_ref, pinned, valid_until=valid_until)
        except RunAlreadyPublishedError:
            return RunOutcome(run_id, "already_published", 0, (), source_ref)
        delivered = self.deliver(run_id, currency)
        try:
            status = self.store.complete_run(run_id)
            pending: tuple = ()
        except IncompleteRunError as exc:
            status = "collecting"
            pending = tuple(exc.pending)
        return RunOutcome(run_id, status, delivered, pending, source_ref)

    def run(self, run_id: str, currency: str = "USD", source_ref: str = "fixture", valid_until=None) -> RunOutcome:
        """One-shot import: the payload itself defines the scope, so a valid
        delivery always covers it and publishes immediately."""
        return self.collect(run_id, currency=currency, source_ref=source_ref, valid_until=valid_until)
