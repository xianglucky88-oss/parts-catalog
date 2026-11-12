"""Tests for the versioned business-quality policy."""
from __future__ import annotations

import pytest

from parts_catalog.quality import (
    ACCEPTED,
    CONTRADICTORY_VALUE,
    LOW_COVERAGE,
    MISSING_FIELD,
    MISSING_IDENTITY,
    QUARANTINED,
    REJECTED,
    QUALITY_POLICY_VERSION,
    QualityPolicy,
    record_fingerprint,
)


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


# --- 1. complete records ----------------------------------------------------
def test_complete_record_is_accepted_with_full_coverage():
    decision = QualityPolicy().evaluate_record(_full_record())
    assert decision.verdict == ACCEPTED
    assert decision.coverage_ratio == 1.0
    assert decision.reasons == ()
    assert decision.policy_version == QUALITY_POLICY_VERSION
    assert decision.part is not None
    assert decision.part.part_number == "AB1204"
    assert decision.part.brand == "ACME"


def test_fixture_records_keep_original_import_semantics():
    """The two shipped fixture rows differ only in quotation attributes."""
    report = QualityPolicy().evaluate_batch(
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
    )
    assert [d.verdict for d in report.decisions] == [ACCEPTED, ACCEPTED]
    profile = report.profiles[0]
    assert profile.supplier == "Northstar"
    assert (profile.total, profile.accepted, profile.quarantined, profile.rejected) == (2, 2, 0, 0)
    assert profile.average_coverage == 1.0


# --- 2. tolerable missing fields -------------------------------------------
def test_single_missing_optional_field_is_tolerated():
    record = _full_record(lead_time_days=None)
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == ACCEPTED
    assert decision.coverage_ratio == round(5 / 6, 4)
    assert [r.field for r in decision.reasons] == ["lead_time_days"]
    assert all(r.code == MISSING_FIELD for r in decision.reasons)


def test_two_missing_optional_fields_still_accepted():
    record = _full_record(lead_time_days=None, price=None)
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == ACCEPTED
    assert decision.coverage_ratio == round(4 / 6, 4)


def test_three_missing_optional_fields_remain_accepted_at_threshold():
    record = _full_record(lead_time_days=None, price=None, description="")
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == ACCEPTED
    assert decision.coverage_ratio == 0.5
    assert all(r.code == MISSING_FIELD for r in decision.reasons)
    assert {r.field for r in decision.reasons} == {"lead_time_days", "price", "description"}


def test_four_missing_optional_fields_quarantine_for_low_coverage():
    record = _full_record(lead_time_days=None, price=None, description="", stock_status=None)
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == QUARANTINED
    assert decision.coverage_ratio == round(2 / 6, 4)
    low = next(r for r in decision.reasons if r.code == LOW_COVERAGE)
    assert low.field == "coverage"
    missing_fields = {r.field for r in decision.reasons if r.code == MISSING_FIELD}
    assert missing_fields == {"lead_time_days", "price", "description", "stock_status"}


def test_unknown_stock_status_counts_as_missing_but_adapts():
    # unknown stock + two more missing => 3/6 missing, coverage 0.5 still accepted
    record = _full_record(stock_status="unknown", lead_time_days=None, price=None)
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == ACCEPTED
    assert decision.part is not None
    assert decision.part.stock_status == "unknown"
    assert {r.field for r in decision.reasons} == {"stock_status", "lead_time_days", "price"}


# --- 3. critical identity anomalies -> rejection ----------------------------
@pytest.mark.parametrize("field_name", ["supplier", "part_number", "brand"])
def test_missing_critical_identity_is_rejected(field_name):
    record = _full_record(**{field_name: None})
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == REJECTED
    assert decision.part is None
    identity_reasons = [r for r in decision.reasons if r.code == MISSING_IDENTITY]
    assert [r.field for r in identity_reasons] == [field_name]


def test_blank_identity_strings_are_rejected():
    decision = QualityPolicy().evaluate_record(_full_record(supplier="   "))
    assert decision.verdict == REJECTED


def test_part_number_without_canonical_characters_is_rejected():
    decision = QualityPolicy().evaluate_record(_full_record(part_number="---"))
    assert decision.verdict == REJECTED
    assert any(r.field == "part_number" and r.code == MISSING_IDENTITY for r in decision.reasons)


def test_rejected_record_never_adapts_even_if_other_fields_broken():
    record = _full_record(brand="", price="not-a-price", package_quantity=0)
    decision = QualityPolicy().evaluate_record(record)
    assert decision.verdict == REJECTED
    assert decision.part is None
    # Identity failure is the decisive reason; field-level detail is retained.
    assert any(r.code == MISSING_IDENTITY and r.field == "brand" for r in decision.reasons)


# --- adaptation failures (identity ok, values broken) -> quarantine ---------
def test_unparseable_price_is_quarantined_with_field_reason():
    decision = QualityPolicy().evaluate_record(_full_record(price="not-a-price"))
    assert decision.verdict == QUARANTINED
    reason = next(r for r in decision.reasons if r.field == "price")
    assert reason.code == "adaptation_error"
    assert decision.part is None


def test_non_positive_package_quantity_is_quarantined():
    decision = QualityPolicy().evaluate_record(_full_record(package_quantity=0))
    assert decision.verdict == QUARANTINED
    assert any(r.field == "package_quantity" and r.code == "adaptation_error" for r in decision.reasons)


# --- 4. contradictory values ------------------------------------------------
def test_conflicting_structural_fields_quarantine_both_records():
    report = QualityPolicy().evaluate_batch(
        [
            _full_record(part_number="AB-1204", package_quantity=1),
            _full_record(part_number="AB1204", package_quantity=5),
        ]
    )
    assert {d.verdict for d in report.decisions} == {QUARANTINED}
    for decision in report.decisions:
        contradiction = [r for r in decision.reasons if r.code == CONTRADICTORY_VALUE]
        assert contradiction and contradiction[0].field == "package_quantity"
        assert contradiction[0].value == 5 or contradiction[0].value == 1


def test_distinct_quote_prices_are_not_contradictions():
    report = QualityPolicy().evaluate_batch(
        [
            _full_record(part_number="AB-1204", price="18.50", lead_time_days=4),
            _full_record(part_number="AB1204", price="21.00", lead_time_days=9),
        ]
    )
    assert all(d.verdict == ACCEPTED for d in report.decisions)
    assert not any(r.code == CONTRADICTORY_VALUE for d in report.decisions for r in d.reasons)


def test_identical_duplicate_inputs_are_not_contradictions():
    record = _full_record()
    report = QualityPolicy().evaluate_batch([dict(record), dict(record)])
    assert all(d.verdict == ACCEPTED for d in report.decisions)
    assert record_fingerprint(record) == report.decisions[0].fingerprint == report.decisions[1].fingerprint


def test_contradiction_across_brands_is_detected():
    report = QualityPolicy().evaluate_batch(
        [
            _full_record(part_number="AB-1204", brand="Acme"),
            _full_record(part_number="AB1204", brand="Globex"),
        ]
    )
    assert all(d.verdict == QUARANTINED for d in report.decisions)
    assert all(any(r.field == "brand" and r.code == CONTRADICTORY_VALUE for r in d.reasons) for d in report.decisions)


# --- profiles ---------------------------------------------------------------
def test_profile_aggregates_verdicts_coverage_and_missing_fields():
    report = QualityPolicy().evaluate_batch(
        [
            _full_record(part_number="A-1"),  # accepted, coverage 1.0
            _full_record(part_number="A-2", lead_time_days=None, price=None),  # accepted 4/6
            _full_record(part_number="A-3", brand=""),  # rejected
            # quarantined: four missing -> coverage 2/6
            _full_record(part_number="A-4", lead_time_days=None, price=None, description="", stock_status=None),
        ]
    )
    profile = report.profiles[0]
    assert profile.total == 4
    assert (profile.accepted, profile.quarantined, profile.rejected) == (2, 1, 1)
    expected_average = round((1.0 + round(4 / 6, 4) + 1.0 + round(2 / 6, 4)) / 4, 4)
    assert profile.average_coverage == expected_average
    assert profile.missing_counts["lead_time_days"] == 2
    assert profile.missing_counts["price"] == 2
    assert profile.missing_counts["brand"] == 1
    serialized = profile.as_dict()
    assert serialized["missing_field_counts"]["description"] == 1
    assert serialized["policy_version"] == QUALITY_POLICY_VERSION


def test_unidentified_supplier_gets_own_profile_bucket():
    report = QualityPolicy().evaluate_batch([_full_record(supplier=""), _full_record(supplier=None)])
    assert all(d.verdict == REJECTED for d in report.decisions)
    assert [p.supplier for p in report.profiles] == ["<unidentified>"]
    assert report.profiles[0].rejected == 2


def test_suppliers_are_profiled_separately():
    report = QualityPolicy().evaluate_batch(
        [_full_record(supplier="Northstar"), _full_record(supplier="Globex", part_number="G-9")]
    )
    assert [p.supplier for p in report.profiles] == ["Globex", "Northstar"]
    assert all(p.total == 1 and p.accepted == 1 for p in report.profiles)


# --- fingerprint / versioning ----------------------------------------------
def test_fingerprint_is_stable_and_content_sensitive():
    record = _full_record()
    assert record_fingerprint(record) == record_fingerprint(dict(record))
    assert record_fingerprint(record) != record_fingerprint(_full_record(price="9.99"))
    assert record_fingerprint(record, "1.0") != record_fingerprint(record, "2.0")
    assert len(record_fingerprint(record)) == 64


def test_report_totals_include_policy_version():
    report = QualityPolicy().evaluate_batch([_full_record(), _full_record(brand="")])
    assert report.totals == {
        "accepted": 1,
        "quarantined": 0,
        "rejected": 1,
        "total": 2,
        "policy_version": QUALITY_POLICY_VERSION,
    }


def test_stricter_policy_quarantines_more_missing_fields():
    strict = QualityPolicy(version="2.0", min_coverage_ratio=0.75)
    decision = strict.evaluate_record(_full_record(lead_time_days=None, price=None))
    assert decision.coverage_ratio == round(4 / 6, 4)
    assert decision.verdict == QUARANTINED
    assert decision.policy_version == "2.0"
    assert any(r.code == LOW_COVERAGE for r in decision.reasons)


def test_empty_batch_yets_empty_report_with_policy_version():
    report = QualityPolicy().evaluate_batch([])
    assert report.decisions == ()
    assert report.profiles == ()
    assert report.totals == {
        "accepted": 0,
        "quarantined": 0,
        "rejected": 0,
        "total": 0,
        "policy_version": QUALITY_POLICY_VERSION,
    }


def test_malformed_numeric_fields_are_quarantined_not_raised():
    decision = QualityPolicy().evaluate_record(
        _full_record(package_quantity="abc", lead_time_days="soon")
    )
    assert decision.verdict == QUARANTINED
    fields = {r.field for r in decision.reasons if r.code == "adaptation_error"}
    assert fields == {"package_quantity", "lead_time_days"}
    assert decision.part is None


def test_na_token_strings_are_treated_as_missing():
    decision = QualityPolicy().evaluate_record(_full_record(price="N/A", description="N/A"))
    missing = {r.field for r in decision.reasons if r.code == MISSING_FIELD}
    assert {"price", "description"} <= missing
    assert decision.verdict == ACCEPTED  # still 4/6 coverage
