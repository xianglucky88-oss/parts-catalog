"""Field-level attribution and merge policy for managed catalog fields.

Every managed catalog field carries an origin ("manual" sign-off from master
data, or "feed" from supplier collection). The merge policy decides, per field
per incoming record, whether the incoming value is applied, kept, or rejected,
and every decision is persisted so a review meeting can see where each value
came from and why it was approved or rejected.
"""
from __future__ import annotations
from dataclasses import dataclass
from .models import NormalizedPart

# Catalog fields governed by the attribution/merge mechanism. Quote facts
# (price, lead time, stock) are deliberately NOT here: they stay independent
# supplier facts in the quotes table and are never adjudicated.
MANAGED_FIELDS = ("brand", "description", "unit", "package_quantity")

# Priority ranks. A lower-rank origin may never overwrite a higher-rank
# incumbent; manual sign-off therefore pins a field against feed rewrites.
ORIGIN_RANK = {"feed": 10, "manual": 100}

# Ledger marker for a record that does not assert a field at all.
MISSING = "<missing>"

OUTCOME_APPLIED = "applied"        # no incumbent; incoming value accepted
OUTCOME_UNCHANGED = "unchanged"    # incoming matches incumbent; no-op
OUTCOME_CONFIRMED = "confirmed"    # same value, higher rank: ownership transfers
OUTCOME_SKIPPED = "skipped"        # record does not assert this field
OUTCOME_OVERRIDDEN = "overridden"  # disagreement auto-resolved: incoming won
OUTCOME_CONFLICT = "conflict"      # disagreement: incoming rejected, incumbent kept


@dataclass(frozen=True)
class FieldFact:
    """One incoming assertion about a managed field. value=None means the
    record does not assert the field (absent in the raw payload)."""
    field: str
    value: object | None
    origin: str
    source_ref: str
    note: str | None = None


@dataclass(frozen=True)
class Incumbent:
    """Currently attributed value of a managed field."""
    value: str
    origin: str
    source_ref: str
    note: str | None = None


@dataclass(frozen=True)
class Decision:
    field: str
    outcome: str
    reason: str
    incoming: FieldFact
    incumbent: Incumbent | None

    @property
    def won(self) -> bool:
        """True when the incoming assertion updates the field's attribution
        (new value, or ownership transfer via confirmation)."""
        return self.outcome in (OUTCOME_APPLIED, OUTCOME_OVERRIDDEN, OUTCOME_CONFIRMED)


def origin_rank(origin: str) -> int:
    try:
        return ORIGIN_RANK[origin]
    except KeyError:
        raise ValueError(f"unknown field origin: {origin!r}") from None


def decide(incumbent: Incumbent | None, fact: FieldFact) -> Decision:
    """Pure merge policy for one managed field. Never mutates anything."""
    origin_rank(fact.origin)  # fail fast on unknown origins
    if fact.value is None:
        return Decision(fact.field, OUTCOME_SKIPPED,
                        "record does not assert this field; incumbent left untouched", fact, incumbent)
    incoming = str(fact.value)
    if incumbent is None:
        return Decision(fact.field, OUTCOME_APPLIED,
                        f"no incumbent on record; accepted from {fact.origin}", fact, None)
    if incoming == incumbent.value:
        if origin_rank(fact.origin) > origin_rank(incumbent.origin):
            # Sign-off that agrees with the current value still takes
            # ownership, so future lower-rank rewrites are rejected.
            return Decision(fact.field, OUTCOME_CONFIRMED,
                            f"{fact.origin} confirms current value; ownership transferred "
                            f"from {incumbent.origin}", fact, incumbent)
        return Decision(fact.field, OUTCOME_UNCHANGED,
                        f"incoming {fact.origin} value matches incumbent", fact, incumbent)
    incoming_rank, incumbent_rank = origin_rank(fact.origin), origin_rank(incumbent.origin)
    if incoming_rank > incumbent_rank:
        return Decision(fact.field, OUTCOME_OVERRIDDEN,
                        f"{fact.origin} outranks {incumbent.origin}; higher-priority value applied",
                        fact, incumbent)
    if incoming_rank < incumbent_rank:
        return Decision(fact.field, OUTCOME_CONFLICT,
                        f"{fact.origin} may not overwrite {incumbent.origin} incumbent; "
                        "incumbent kept, flagged for review", fact, incumbent)
    return Decision(fact.field, OUTCOME_OVERRIDDEN,
                    f"same origin rank ({fact.origin}); latest record wins, flagged for review",
                    fact, incumbent)


def facts_from_record(part: NormalizedPart, raw: dict, origin: str,
                      source_ref: str, note: str | None) -> list[FieldFact]:
    """Build per-field facts from a normalized part plus its raw record.

    Presence is judged on the raw payload, not the normalized part, so that
    contract defaults (unit "EA", package_quantity 1, description "") are
    never mistaken for supplier assertions: a field absent from the raw
    record yields a missing fact and is skipped by the merge.
    """
    origin_rank(origin)
    values = {
        "brand": part.brand,
        "description": part.description,
        "unit": part.unit,
        "package_quantity": part.package_quantity,
    }
    facts = []
    for field in MANAGED_FIELDS:
        asserted = field in raw and raw[field] is not None
        facts.append(FieldFact(field, values[field] if asserted else None,
                               origin, source_ref, note))
    return facts
