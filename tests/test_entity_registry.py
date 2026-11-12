from __future__ import annotations

import json

import pytest

from parts_catalog.entity_registry import (
    CONFLICT,
    PENDING,
    RESOLVED,
    SupplierRegistry,
    normalize_supplier_name,
)
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource, JsonHandoverSource, apply_handover
from parts_catalog.storage import PartsStore


@pytest.fixture
def registry():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    yield SupplierRegistry(connection)
    connection.close()


def test_case_variants_resolve_to_one_entity(registry):
    first = registry.observe("Acme Trading", "portal/2026-11")
    first_decision = registry.current_decision(first.observation_id)
    assert first_decision.state == RESOLVED
    assert first_decision.reason == "new_entity_no_similar_existing"

    second = registry.observe("ACME trading", "portal/2026-11")
    second_decision = registry.current_decision(second.observation_id)
    assert second_decision.state == RESOLVED
    assert second_decision.entity_id == first_decision.entity_id
    assert second_decision.reason == "case_variant_of_existing_name"

    assert len(registry.entities()) == 1
    evidence = registry.evidence_for(second.observation_id)
    assert [item.kind for item in evidence] == ["case_variant"]
    assert evidence[0].strength == "strong"


def test_confirmed_alias_and_repeated_handover_are_idempotent(registry):
    entity = registry.create_entity("SUP-NORTHSTAR", "Northstar Industrial Group")
    registry.register_alias(
        "NS Trading",
        entity.entity_id,
        basis="old_portal_account",
        source_ref="handover/2026-11",
    )

    # Receiving the same handover material again must not duplicate anything.
    for _ in range(2):
        registry.create_entity("SUP-NORTHSTAR", "Northstar Industrial Group")
        registry.register_alias(
            "NS Trading",
            entity.entity_id,
            basis="old_portal_account",
            source_ref="handover/2026-11",
        )
    assert len(registry.alias_claims()) == 1

    observation = registry.observe("ns trading", "portal/2026-11")
    decision = registry.current_decision(observation.observation_id)
    assert decision.state == RESOLVED
    assert decision.entity_id == entity.entity_id
    assert decision.reason == "confirmed_alias_claim"
    evidence = registry.evidence_for(observation.observation_id)
    assert [item.kind for item in evidence] == ["confirmed_alias"]
    assert evidence[0].claim_id == registry.alias_claims()[0].claim_id


def test_conflicting_alias_claims_stay_in_conflict(registry):
    left = registry.create_entity("SUP-MERIDIAN-A", "Meridian Components A")
    right = registry.create_entity("SUP-MERIDIAN-B", "Meridian Holdings B")
    for entity in (left, right):
        registry.register_alias(
            "Meridian",
            entity.entity_id,
            basis="regional_alias",
            source_ref="handover/2026-11",
        )

    observation = registry.observe("Meridian", "portal/2026-11")
    decision = registry.current_decision(observation.observation_id)
    assert decision.state == CONFLICT
    assert decision.entity_id is None
    assert decision.reason == "conflicting_alias_claims"

    evidence = registry.evidence_for(observation.observation_id)
    assert {item.entity_id for item in evidence} == {left.entity_id, right.entity_id}
    assert all(item.kind == "conflicting_alias_claim" for item in evidence)

    repeated = registry.observe("Meridian", "portal/2026-11")
    assert repeated.observation_id == observation.observation_id
    assert len(registry.decisions_for(observation.observation_id)) == 1
    assert len(registry.evidence_for(observation.observation_id)) == 2


def test_empty_supplier_name_is_pending_and_never_accumulates(registry):
    observation = registry.observe("", "portal/2026-11")
    decision = registry.current_decision(observation.observation_id)
    assert decision.state == PENDING
    assert decision.entity_id is None
    assert decision.reason == "empty_supplier_name"
    assert registry.evidence_for(observation.observation_id) == []
    assert registry.entities() == []

    for _ in range(3):
        repeated = registry.observe("", "portal/2026-11")
    assert repeated.observation_id == observation.observation_id
    assert len(registry.observations()) == 1
    assert len(registry.decisions_for(observation.observation_id)) == 1


def test_similar_name_is_pending_not_auto_merged(registry):
    entity = registry.create_entity("SUP-NORTHSTAR", "Northstar Industrial Group")
    registry.register_alias(
        "Northstar",
        entity.entity_id,
        basis="new_group_name",
        source_ref="handover/2026-11",
    )

    observation = registry.observe("North Star", "portal/2026-11")
    decision = registry.current_decision(observation.observation_id)
    assert decision.state == PENDING
    assert decision.entity_id is None
    assert decision.reason == "similar_name_requires_review"

    evidence = registry.evidence_for(observation.observation_id)
    assert all(item.strength == "weak" for item in evidence)
    assert {item.entity_id for item in evidence} == {entity.entity_id}
    assert len(registry.entities()) == 1


def test_normalizer_does_not_strip_legal_names(registry):
    assert normalize_supplier_name("Acme  Trading") == "acme trading"
    assert normalize_supplier_name("ACME TRADING") == "acme trading"
    # Legal suffixes and punctuation stay: similar is not the same.
    assert normalize_supplier_name("Acme Trading, Inc.") != "acme trading"


def test_decisions_are_traceable_and_revocable(registry):
    observation = registry.observe("Acme Trading", "portal/2026-11")
    decision = registry.current_decision(observation.observation_id)
    assert decision.state == RESOLVED

    replacement = registry.revoke(decision.decision_id, revoked_by="procurement-reviewer")
    assert replacement.state == PENDING
    assert replacement.reason == "revoked_decision_requires_review"
    assert replacement.is_hold
    assert replacement.supersedes_decision_id == decision.decision_id

    history = registry.decisions_for(observation.observation_id)
    assert len(history) == 2
    assert history[0].revoked_at is not None
    assert history[0].revoked_by == "procurement-reviewer"
    assert history[0].superseded_by_decision_id == replacement.decision_id

    # A manual hold is sticky: automatic evaluation cannot recreate the decision.
    assert registry.evaluate(observation.observation_id).decision_id == replacement.decision_id

    manual = registry.resolve_manually(
        observation.observation_id,
        RESOLVED,
        decision.entity_id,
        note="confirmed by procurement after alias review",
    )
    assert manual.state == RESOLVED
    assert manual.decided_by == "human"
    assert manual.reason == "confirmed by procurement after alias review"

    assert registry.evaluate(observation.observation_id).decision_id == manual.decision_id


def test_pipeline_records_entity_identity_separately_from_raw_name(tmp_path):
    store = PartsStore(tmp_path / "parts.sqlite3")
    registry = SupplierRegistry(store.connection)
    handover_path = "fixtures/supplier_handover.json"
    apply_handover(registry, JsonHandoverSource(handover_path).read(), handover_path)
    pipeline = PartsPipeline(JsonFixtureSource(handover_path), store, registry)

    pipeline.run(source_ref=handover_path)
    rows = store.connection.execute(
        "SELECT supplier, supplier_entity_id FROM quotes ORDER BY supplier"
    ).fetchall()
    by_supplier = {supplier: entity for supplier, entity in rows}

    assert by_supplier["Northstar"] == "SUP-NORTHSTAR"
    assert by_supplier["NS Trading"] == "SUP-NORTHSTAR"
    assert by_supplier["ACME trading"] == by_supplier["Acme Trading"]
    assert by_supplier["Meridian"] is None
    assert by_supplier[""] is None
    assert by_supplier["North Star"] is None

    # The stored identity is the stable entity id, never the raw name.
    assert by_supplier["Northstar"] != "Northstar"

    observations_before = len(registry.observations())
    decisions_before = sum(
        len(registry.decisions_for(item.observation_id))
        for item in registry.observations()
    )
    evidence_before = sum(
        len(registry.evidence_for(item.observation_id))
        for item in registry.observations()
    )

    # Receiving the same material again changes nothing.
    pipeline.run(source_ref=handover_path)
    apply_handover(registry, JsonHandoverSource(handover_path).read(), handover_path)
    assert len(registry.observations()) == observations_before
    assert len(registry.alias_claims()) == 4
    assert sum(
        len(registry.decisions_for(item.observation_id))
        for item in registry.observations()
    ) == decisions_before
    assert sum(
        len(registry.evidence_for(item.observation_id))
        for item in registry.observations()
    ) == evidence_before

    store.close()
