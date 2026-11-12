"""End-to-end import tests: quality gate, persistence and replay idempotency."""
from __future__ import annotations

import pytest

from parts_catalog.pipeline import PartsPipeline
from parts_catalog.quality import (
    ACCEPTED,
    QUARANTINED,
    REJECTED,
    QUALITY_POLICY_VERSION,
    QualityPolicy,
)
from parts_catalog.storage import PartsStore


class ListSource:
    def __init__(self, records):
        self._records = records

    def read(self):
        return list(self._records)


def _full_record(**overrides):
    record = {
        "supplier": "Northstar",
        "part_number": "AB-120 / 4",
        "brand": "Acme Inc.",
        "description": "Sealed bearing 20 mm",
        "unit": "ea",
        "package_quantity": 1,
        "lead_time_days": 4,
        "stock_status": "In Stock",
        "price": "18.50",
    }
    record.update(overrides)
    return record


@pytest.fixture()
def store(tmp_path):
    db = PartsStore(tmp_path / "parts.sqlite3")
    yield db
    db.close()


def test_accepted_records_persist_catalog_and_quote(store):
    record = _full_record()
    report = PartsPipeline(ListSource([record]), store).run_with_report(source_ref="run-1")
    assert report.totals["accepted"] == 1
    assert store.count("parts") == 1
    assert store.count("quotes") == 1
    assert store.count("quality_decisions") == 0
    quote = store.connection.execute("SELECT currency, price, source_ref FROM quotes").fetchone()
    assert quote == ("USD", "18.50", "run-1")


def test_quarantined_record_is_persisted_with_field_reasons_but_not_imported(store):
    record = _full_record(lead_time_days=None, price=None, description="", stock_status=None)
    PartsPipeline(ListSource([record]), store).run_with_report(source_ref="run-1")
    assert store.count("parts") == 0
    assert store.count("quotes") == 0
    decisions = store.quality_decisions()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["verdict"] == QUARANTINED
    assert decision["policy_version"] == QUALITY_POLICY_VERSION
    reason_codes = {r["code"] for r in decision["reasons"]}
    assert "low_coverage" in reason_codes
    assert all("field" in r and "message" in r for r in decision["reasons"])
    assert decision["raw_record"]["supplier"] == "Northstar"


def test_rejected_record_is_persisted_and_never_touches_business_tables(store):
    record = _full_record(part_number=None)
    PartsPipeline(ListSource([record]), store).run_with_report(source_ref="run-1")
    assert store.count("parts") == 0
    assert store.count("quotes") == 0
    decisions = store.quality_decisions()
    assert len(decisions) == 1
    assert decisions[0]["verdict"] == REJECTED
    assert any(r["code"] == "missing_identity" and r["field"] == "part_number" for r in decisions[0]["reasons"])


def test_contradictory_batch_quarantines_and_profiles_are_stored(store):
    records = [
        _full_record(part_number="AB-1204", package_quantity=1),
        _full_record(part_number="AB1204", package_quantity=5),
    ]
    report = PartsPipeline(ListSource(records), store).run_with_report(source_ref="run-1")
    assert report.totals["quarantined"] == 2
    assert store.count("quality_decisions") == 2
    profiles = store.quality_profiles()
    assert len(profiles) == 1
    assert profiles[0]["total"] == 2
    assert profiles[0]["quarantined"] == 2
    assert profiles[0]["detail"]["missing_field_counts"] == {}


def test_replay_same_input_creates_no_duplicate_rows(store):
    records = [
        _full_record(part_number="A-1"),
        _full_record(part_number="A-2", brand=""),  # rejected
        _full_record(
            part_number="A-3",
            lead_time_days=None,
            price=None,
            description="",
            stock_status=None,
        ),  # quarantined (4 missing -> low coverage)
    ]
    pipeline = PartsPipeline(ListSource(records), store)
    first = pipeline.run_with_report(source_ref="fixture-x")
    counts_after_first = {
        "parts": store.count("parts"),
        "quotes": store.count("quotes"),
        "quality_decisions": store.count("quality_decisions"),
        "quality_profiles": store.count("quality_profiles"),
    }
    assert counts_after_first == {"parts": 1, "quotes": 1, "quality_decisions": 2, "quality_profiles": 1}

    # Replay the identical payload through a fresh pipeline/store connection.
    store.commit()
    second = PartsPipeline(ListSource([dict(r) for r in records]), store).run_with_report(source_ref="fixture-x")
    assert [d.verdict for d in first.decisions] == [d.verdict for d in second.decisions]
    assert {
        "parts": store.count("parts"),
        "quotes": store.count("quotes"),
        "quality_decisions": store.count("quality_decisions"),
        "quality_profiles": store.count("quality_profiles"),
    } == counts_after_first


def test_replay_keeps_single_quarantine_row_per_content_fingerprint(store):
    record = _full_record(price="bad")
    source = ListSource([record])
    PartsPipeline(source, store).run_with_report(source_ref="run-1")
    PartsPipeline(ListSource([dict(record)]), store).run_with_report(source_ref="run-1")
    decisions = store.quality_decisions()
    assert len(decisions) == 1
    assert decisions[0]["verdict"] == QUARANTINED


def test_identical_duplicate_rows_in_one_batch_do_not_duplicate_on_persistence(store):
    record = _full_record(part_number="A-1")
    PartsPipeline(ListSource([dict(record), dict(record)]), store).run_with_report(source_ref="run-1")
    assert store.count("parts") == 1
    assert store.count("quotes") == 1  # same PK (canonical_number, supplier, source_ref)


def test_profile_upsert_reflects_latest_run_without_duplicate_rows(store):
    source_one = ListSource([_full_record(part_number="A-1"), _full_record(part_number="A-2", brand="")])
    PartsPipeline(source_one, store).run_with_report(source_ref="same-ref")
    assert store.quality_profiles()[0]["rejected"] == 1

    source_two = ListSource([_full_record(part_number="A-1")])
    PartsPipeline(source_two, store).run_with_report(source_ref="same-ref")
    profiles = store.quality_profiles()
    assert len(profiles) == 1
    assert profiles[0]["total"] == 1
    assert profiles[0]["accepted"] == 1
    assert profiles[0]["rejected"] == 0


def test_run_returns_number_of_evaluated_records(store):
    records = [_full_record(), _full_record(brand="")]
    count = PartsPipeline(ListSource(records), store).run(source_ref="run-1")
    assert count == 2


def test_custom_policy_version_is_propagated_to_storage(store):
    policy = QualityPolicy(version="9.9", min_coverage_ratio=0.75)
    PartsPipeline(ListSource([_full_record(lead_time_days=None)]), store, policy=policy).run_with_report(
        source_ref="run-1"
    )
    assert store.quality_profiles()[0]["policy_version"] == "9.9"


def test_shipped_fixture_imports_both_records(tmp_path):
    import json

    fixture = tmp_path / "payload.json"
    fixture.write_text(
        json.dumps(
            [
                _full_record(lead_time_days=4, price="18.50", description="Sealed bearing 20 mm"),
                _full_record(
                    part_number="AB1204",
                    brand="ACME",
                    description="Sealed bearing 20mm",
                    unit="EA",
                    lead_time_days=5,
                    stock_status="Available",
                    price="18.75",
                ),
            ]
        ),
        encoding="utf-8",
    )
    from parts_catalog.sources import JsonFixtureSource

    db = PartsStore(tmp_path / "fixture.sqlite3")
    try:
        report = PartsPipeline(JsonFixtureSource(fixture), db).run_with_report(source_ref=str(fixture))
        assert report.totals["accepted"] == 2
        assert db.count("parts") == 1  # same canonical number -> one catalog row
        assert db.count("quotes") == 1  # same (canonical, supplier, source_ref) PK
        assert db.count("quality_decisions") == 0
    finally:
        db.close()
