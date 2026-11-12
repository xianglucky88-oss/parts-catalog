import pytest
from parts_catalog.merge import FieldFact, Incumbent, decide


def fact(value, origin="feed"):
    return FieldFact("package_quantity", value, origin, "src")


def test_missing_value_is_skipped_and_never_erases():
    decision = decide(Incumbent("10", "manual", "signoff"), fact(None))
    assert decision.outcome == "skipped"
    assert not decision.won


def test_empty_field_is_applied_with_origin():
    decision = decide(None, fact("10"))
    assert decision.outcome == "applied"
    assert decision.won


def test_matching_value_is_unchanged_noop():
    decision = decide(Incumbent("10", "feed", "a"), fact("10"))
    assert decision.outcome == "unchanged"
    assert not decision.won


def test_manual_incumbent_rejects_feed_overwrite_as_reviewable_conflict():
    decision = decide(Incumbent("10", "manual", "signoff"), fact("20"))
    assert decision.outcome == "conflict"
    assert not decision.won
    assert "manual" in decision.reason


def test_manual_overrides_feed_incumbent():
    decision = decide(Incumbent("10", "feed", "a"), fact("20", "manual"))
    assert decision.outcome == "overridden"
    assert decision.won


def test_manual_confirmation_of_same_value_transfers_ownership():
    decision = decide(Incumbent("10", "feed", "a"), fact("10", "manual"))
    assert decision.outcome == "confirmed"
    assert decision.won
    # ...while a feed echo of a manual value stays a plain no-op.
    assert decide(Incumbent("10", "manual", "m"), fact("10")).outcome == "unchanged"


def test_same_rank_disagreement_latest_wins_and_is_flagged():
    decision = decide(Incumbent("10", "feed", "a"), fact("20"))
    assert decision.outcome == "overridden"
    assert decision.won
    assert "review" in decision.reason


def test_unknown_origin_is_rejected():
    with pytest.raises(ValueError):
        decide(None, fact("10", "vendor"))
