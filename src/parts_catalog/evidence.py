"""Field-level transformation evidence ledger.

The ledger is an append-only, hash-chained record of every transformation
that produced or overwrote a catalog part or a quote. Each entry binds:

* the input record (content hash + position inside the import),
* the exact normalization rule (rule id + rule-set version),
* the business entity as it existed before and after the write,
* its position in a global monotonic sequence,
* the previous entry's hash, making any deletion, reorder or rewrite
  detectable when the chain is re-verified.

Hashes are computed over canonical JSON with sorted keys so verification is
independent of dict insertion order.
"""

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

LEDGER_SCHEMA_VERSION = "evidence-ledger-1"
RUN_COMMITTED = "committed"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def input_digest(raw_records: Iterable[dict]) -> str:
    """Content hash of the complete import input.

    The hash covers every record in its original order, so two imports have
    the same digest only when their inputs are byte-for-byte equivalent as
    parsed JSON.
    """
    return digest(list(raw_records))


def make_run_id(input_hash: str, source_ref: str, currency: str, rule_set_version: str) -> str:
    """Deterministic identity of one logical import.

    Replaying the same import yields the same run id and therefore cannot
    create a second set of business facts or evidence.
    """
    return digest(
        {
            "input_hash": input_hash,
            "source_ref": source_ref,
            "currency": currency,
            "rule_set_version": rule_set_version,
        }
    )


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    entry_id: str
    run_id: str
    input_hash: str
    raw_index: int
    raw_record_hash: str
    entity_type: str
    entity_key: str
    rule_set_version: str
    rule_ids: list[str]
    transforms: list[dict]
    before_snapshot: dict | None
    after_snapshot: dict | None
    prev_hash: str
    entry_hash: str


def compute_entry_hash(
    seq: int,
    entry_id: str,
    run_id: str,
    input_hash: str,
    raw_index: int,
    raw_record_hash: str,
    entity_type: str,
    entity_key: str,
    rule_set_version: str,
    rule_ids: list[str],
    transforms: list[dict],
    before_snapshot: dict | None,
    after_snapshot: dict | None,
    prev_hash: str,
) -> str:
    payload = {
        "schema": LEDGER_SCHEMA_VERSION,
        "seq": seq,
        "entry_id": entry_id,
        "run_id": run_id,
        "input_hash": input_hash,
        "raw_index": raw_index,
        "raw_record_hash": raw_record_hash,
        "entity_type": entity_type,
        "entity_key": entity_key,
        "rule_set_version": rule_set_version,
        "rule_ids": rule_ids,
        "transforms": transforms,
        "before_snapshot": before_snapshot,
        "after_snapshot": after_snapshot,
        "prev_hash": prev_hash,
    }
    return digest(payload)


def make_entry(
    seq: int,
    run_id: str,
    input_hash: str,
    raw_index: int,
    raw_record: dict,
    entity_type: str,
    entity_key: str,
    rule_set_version: str,
    rule_ids: list[str],
    transforms: list[dict],
    before_snapshot: dict | None,
    after_snapshot: dict | None,
    prev_hash: str,
) -> LedgerEntry:
    raw_record_hash = digest(raw_record)
    # Deterministic per-entry identity: replaying the same transformation at
    # the same chain position would reproduce the same entry id.
    entry_id = digest(
        {
            "run_id": run_id,
            "seq": seq,
            "entity_type": entity_type,
            "entity_key": entity_key,
            "raw_record_hash": raw_record_hash,
        }
    )
    entry_hash = compute_entry_hash(
        seq,
        entry_id,
        run_id,
        input_hash,
        raw_index,
        raw_record_hash,
        entity_type,
        entity_key,
        rule_set_version,
        rule_ids,
        transforms,
        before_snapshot,
        after_snapshot,
        prev_hash,
    )
    return LedgerEntry(
        seq=seq,
        entry_id=entry_id,
        run_id=run_id,
        input_hash=input_hash,
        raw_index=raw_index,
        raw_record_hash=raw_record_hash,
        entity_type=entity_type,
        entity_key=entity_key,
        rule_set_version=rule_set_version,
        rule_ids=rule_ids,
        transforms=transforms,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
