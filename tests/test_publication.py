from __future__ import annotations
from decimal import Decimal

import pytest

from parts_catalog.errors import (
    DeliveryError,
    IncompleteRunError,
    OutOfScopeError,
    RunStateError,
)
from parts_catalog.models import ScopeEntry
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.storage import PartsStore

FUTURE = "2099-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"

SCOPE = [
    ScopeEntry("Northstar", "AB1204"),
    ScopeEntry("Northstar", "CS770"),
    ScopeEntry("Acme Direct", "AB1204"),
    ScopeEntry("Acme Direct", "CS770"),
]

BASELINE = [
    {"supplier": "Northstar", "part_number": "AB1204", "brand": "ACME", "description": "Sealed bearing 20mm", "package_quantity": 1, "lead_time_days": 5, "stock_status": "Available", "price": "18.75"},
    {"supplier": "Northstar", "part_number": "CS770", "brand": "Northstar Ltd", "description": "Ceramic capacitor 47uF", "package_quantity": 10, "lead_time_days": 2, "stock_status": "In Stock", "price": "9.40"},
    {"supplier": "Acme Direct", "part_number": "AB1204", "brand": "Acme Inc.", "description": "Sealed bearing 20 mm", "package_quantity": 1, "lead_time_days": 7, "stock_status": "Backorder", "price": "19.10"},
    {"supplier": "Acme Direct", "part_number": "CS770", "brand": "Northstar Ltd", "description": "Ceramic capacitor 47uF", "package_quantity": 10, "lead_time_days": 3, "stock_status": "Available", "price": "9.05"},
]

PARTIAL = BASELINE[:2]  # the overnight run only finished the Northstar half

REMAINING = [
    {"supplier": "Acme Direct", "part_number": "AB1204", "brand": "Acme Inc.", "description": "Sealed bearing 20 mm", "package_quantity": 1, "lead_time_days": 8, "stock_status": "Backorder", "price": "19.30"},
    # explicit supplier answer: no quote offered (price None, out of stock)
    {"supplier": "Acme Direct", "part_number": "CS770", "brand": "Northstar Ltd", "description": "Ceramic capacitor 47uF", "package_quantity": 10, "lead_time_days": 4, "stock_status": "Out of Stock", "price": None},
]

FAULTY = [
    PARTIAL[0],
    {**PARTIAL[1], "price": "not-a-number"},
    {**REMAINING[0], "package_quantity": 0},
]


class ListSource:
    def __init__(self, records):
        self.records = records

    def read(self):
        return self.records


@pytest.fixture
def store(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    yield store
    store.close()


def pipeline_for(records, store):
    return PartsPipeline(ListSource(records), store)


def view_map(store, now=None):
    return {(q.supplier, q.canonical_number): q for q in store.visible_quotes(now=now)}


def publish_baseline(store, run_id="night-0", source_ref="feed", valid_until=FUTURE):
    outcome = pipeline_for(BASELINE, store).collect(run_id, source_ref=source_ref, scope=SCOPE, valid_until=valid_until)
    assert outcome.status == "published"
    return outcome


def test_partial_collection_keeps_previous_complete_view(store):
    """The incident: the overnight run covers only half the pinned queries.
    Procurement must keep seeing the previous complete round — suppliers that
    were not re-queried must not show up as having no quote."""
    publish_baseline(store)
    before = store.visible_quotes()
    assert len(before) == 4

    outcome = pipeline_for(PARTIAL, store).collect("night-1", source_ref="feed", scope=SCOPE)
    assert outcome.status == "collecting"
    assert {(e.supplier, e.canonical_number) for e in outcome.pending} == {
        ("Acme Direct", "AB1204"),
        ("Acme Direct", "CS770"),
    }

    after = store.visible_quotes()
    assert after == before
    assert all(q.run_id == "night-0" for q in after)
    assert all(q.price is not None for q in after if q.supplier == "Acme Direct")


def test_complete_run_requires_full_scope_coverage(store):
    pipeline = pipeline_for(PARTIAL, store)
    pipeline.begin_run("run-1", "feed", SCOPE)
    pipeline.deliver("run-1")
    with pytest.raises(IncompleteRunError) as excinfo:
        store.complete_run("run-1")
    assert {(e.supplier, e.canonical_number) for e in excinfo.value.pending} == {
        ("Acme Direct", "AB1204"),
        ("Acme Direct", "CS770"),
    }
    assert store.visible_quotes() == []


def test_publish_replaces_only_cells_inside_pinned_scope(store):
    publish_baseline(store, run_id="run-1")
    northstar_only = SCOPE[:2]
    refreshed = [
        {**BASELINE[0], "price": "18.90"},
        {**BASELINE[1], "price": "9.55"},
    ]
    outcome = pipeline_for(refreshed, store).collect("run-2", source_ref="feed", scope=northstar_only)
    assert outcome.status == "published"

    view = view_map(store)
    assert len(view) == 4
    assert view[("Northstar", "AB1204")].run_id == "run-2"
    assert view[("Northstar", "AB1204")].price == Decimal("18.90")
    assert view[("Northstar", "CS770")].price == Decimal("9.55")
    # cells outside the pinned scope keep their previous publication
    assert view[("Acme Direct", "AB1204")].run_id == "run-1"
    assert view[("Acme Direct", "AB1204")].price == Decimal("19.10")


def test_explicit_no_quote_differs_from_unanswered_query(store):
    publish_baseline(store)
    outcome = pipeline_for(PARTIAL + REMAINING, store).collect("night-1", source_ref="feed", scope=SCOPE)
    assert outcome.status == "published"

    view = view_map(store)
    explicit_no_quote = view[("Acme Direct", "CS770")]
    assert explicit_no_quote.price is None
    assert explicit_no_quote.stock_status == "unavailable"
    assert explicit_no_quote.run_id == "night-1"


def test_repeated_delivery_is_idempotent(store):
    pipeline = pipeline_for(PARTIAL, store)
    pipeline.begin_run("run-1", "feed", SCOPE)
    assert pipeline.deliver("run-1") == 2
    assert pipeline.deliver("run-1") == 2  # same batch retransmitted
    assert pipeline_for(PARTIAL[:1], store).deliver("run-1") == 1  # subset again
    assert store.run_info("run-1").staged_queries == 2
    # staged data is never visible before the run publishes
    assert store.visible_quotes() == []


def test_starting_new_run_leaves_visible_view_untouched(store):
    publish_baseline(store, run_id="run-1")
    before = store.visible_quotes()

    pipeline = pipeline_for(PARTIAL, store)
    pipeline.begin_run("run-2", "feed", SCOPE)
    assert store.visible_quotes() == before
    # re-beginning the same run with the same scope is a safe no-op
    pipeline.begin_run("run-2", "feed", SCOPE)
    assert store.run_info("run-2").status == "collecting"
    assert store.visible_quotes() == before


def test_failed_run_keeps_staged_state_for_safe_retry(store):
    publish_baseline(store)
    pipeline = pipeline_for(PARTIAL, store)
    pipeline.begin_run("night-1", "feed", SCOPE)
    pipeline.deliver("night-1")
    store.fail_run("night-1")

    info = store.run_info("night-1")
    assert info.status == "failed"
    assert info.staged_queries == 2
    assert {(e.supplier, e.canonical_number) for e in store.pending_queries("night-1")} == {
        ("Acme Direct", "AB1204"),
        ("Acme Direct", "CS770"),
    }
    # the failed run never disturbed the visible view
    assert all(q.run_id == "night-0" for q in store.visible_quotes())

    # retry: resume the SAME run id, deliver only what was missing
    retry = pipeline_for(REMAINING, store)
    retry.begin_run("night-1", "feed", SCOPE)
    assert store.run_info("night-1").status == "collecting"
    retry.deliver("night-1")
    assert store.complete_run("night-1") == "published"
    assert len(store.visible_quotes()) == 4


def test_faulty_delivery_is_rejected_and_the_run_stays_retryable(store):
    pipeline = pipeline_for(FAULTY, store)
    pipeline.begin_run("run-1", "feed", SCOPE)
    with pytest.raises(DeliveryError) as excinfo:
        pipeline.deliver("run-1")
    message = str(excinfo.value)
    assert "record 1" in message and "record 2" in message
    # the batch is atomic: even the valid first record was not staged
    assert store.run_info("run-1").staged_queries == 0
    assert store.run_info("run-1").status == "collecting"

    outcome = pipeline_for(BASELINE, store).collect("run-1", source_ref="feed", scope=SCOPE)
    assert outcome.status == "published"
    assert len(store.visible_quotes()) == 4


def test_delivery_outside_pinned_scope_is_rejected(store):
    pipeline = pipeline_for(BASELINE, store)
    pipeline.begin_run("run-1", "feed", SCOPE[:2])
    with pytest.raises(OutOfScopeError):
        pipeline.deliver("run-1")
    assert store.run_info("run-1").staged_queries == 0


def test_delivery_requires_begun_run(store):
    with pytest.raises(RunStateError):
        pipeline_for(PARTIAL, store).deliver("never-begun")


def test_published_run_is_immutable(store):
    publish_baseline(store, run_id="run-1")
    pipeline = pipeline_for(BASELINE, store)
    with pytest.raises(RunStateError):
        pipeline.begin_run("run-1", "feed", SCOPE)
    with pytest.raises(RunStateError):
        pipeline.deliver("run-1")
    # repeating a finished round through collect() is a reported no-op
    outcome = pipeline.collect("run-1", source_ref="feed", scope=SCOPE)
    assert outcome.status == "already_published"
    assert all(q.run_id == "run-1" for q in store.visible_quotes())


def test_begin_run_with_conflicting_scope_or_source_is_rejected(store):
    pipeline = pipeline_for(PARTIAL, store)
    pipeline.begin_run("run-1", "feed", SCOPE)
    with pytest.raises(RunStateError):
        pipeline.begin_run("run-1", "feed", SCOPE[:2])
    with pytest.raises(RunStateError):
        pipeline.begin_run("run-1", "other-feed", SCOPE)


def test_expired_publication_is_marked_not_hidden(store):
    publish_baseline(store, valid_until=PAST)
    view = store.visible_quotes()
    assert len(view) == 4
    assert all(q.visibility == "expired" for q in view)


def test_current_publication_within_validity_window(store):
    publish_baseline(store, valid_until=FUTURE)
    assert all(q.visibility == "current" for q in store.visible_quotes())


def test_publication_without_expiry_stays_current(store):
    publish_baseline(store, valid_until=None)
    assert all(q.visibility == "current" for q in store.visible_quotes())


def test_expiring_a_source_is_deterministic(store):
    publish_baseline(store, run_id="run-1", source_ref="feed-a", valid_until=None)
    refreshed = [{**BASELINE[0], "price": "18.90"}, {**BASELINE[1], "price": "9.55"}]
    pipeline_for(refreshed, store).collect("run-2", source_ref="feed-b", scope=SCOPE[:2])

    assert store.expire_source("feed-a") == 2
    view = view_map(store)
    assert len(view) == 4
    # cells republished by feed-b stay current; the rest of feed-a is expired
    assert view[("Northstar", "AB1204")].visibility == "current"
    assert view[("Northstar", "CS770")].visibility == "current"
    assert view[("Acme Direct", "AB1204")].visibility == "expired"
    assert view[("Acme Direct", "CS770")].visibility == "expired"
    # expiring again is a no-op
    assert store.expire_source("feed-a") == 0


def test_one_shot_run_publishes_and_collapses_duplicate_cells(store):
    payload = [
        {"supplier": "Northstar", "part_number": "AB-120 / 4", "brand": "Acme Inc.", "description": "Sealed bearing 20 mm", "package_quantity": 1, "lead_time_days": 4, "stock_status": "In Stock", "price": "18.50"},
        {"supplier": "Northstar", "part_number": "AB1204", "brand": "ACME", "description": "Sealed bearing 20mm", "package_quantity": 1, "lead_time_days": 5, "stock_status": "Available", "price": "18.75"},
    ]
    outcome = pipeline_for(payload, store).run("one-shot", source_ref="fixture")
    assert outcome.status == "published"
    view = store.visible_quotes()
    assert len(view) == 1
    assert view[0].price == Decimal("18.75")  # last record in the batch wins
    # repeating the identical one-shot run changes nothing
    assert pipeline_for(payload, store).run("one-shot", source_ref="fixture").status == "already_published"
