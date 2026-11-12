"""Verification and inspection tools for the evidence ledger.

The verifier recomputes every hash from the stored record content and checks:

1. chain integrity   -- each entry's ``prev_hash`` equals the previous
   entry's recomputed hash, and the entry hash itself matches;
2. sequence integrity -- ``seq`` is gapless and strictly increasing, which is
   what makes a deleted (missing) or reordered entry visible;
3. state consistency -- every entry's ``after_snapshot`` either equals the
   current row in the business table (the latest write wins) or equals the
   ``before_snapshot`` of the next entry for the same entity (an overwrite
   that the ledger itself accounts for);
4. run binding -- every ledger entry belongs to a recorded, committed import
   run, and each run's input digest is referenced by exactly its own entries.

Each finding names the first broken sequence number and, for hash/sequence
breaks, the blast radius: every subsequent entry is unreachable from the
genesis link, so tampering cannot be hidden behind later valid rows.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field

from .evidence import LEDGER_SCHEMA_VERSION, compute_entry_hash, digest
from .storage import GENESIS_HASH, PartsStore, quote_entity_key


@dataclass
class Finding:
    code: str
    seq: int | None
    message: str
    blast_radius: list[int] = field(default_factory=list)


@dataclass
class AuditReport:
    ok: bool
    entries_checked: int
    runs_checked: int
    findings: list[Finding]
    chain_head_seq: int

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "runs_checked": self.runs_checked,
            "chain_head_seq": self.chain_head_seq,
            "findings": [
                {"code": f.code, "seq": f.seq, "message": f.message, "blast_radius": f.blast_radius}
                for f in self.findings
            ],
        }


def _loads(value):
    return json.loads(value) if value is not None else None


def _recompute_hash(row: dict) -> str:
    return compute_entry_hash(
        seq=row["seq"],
        entry_id=row["entry_id"],
        run_id=row["run_id"],
        input_hash=row["input_hash"],
        raw_index=row["raw_index"],
        raw_record_hash=row["raw_record_hash"],
        entity_type=row["entity_type"],
        entity_key=row["entity_key"],
        rule_set_version=row["rule_set_version"],
        rule_ids=_loads(row["rule_ids"]),
        transforms=_loads(row["transforms"]),
        before_snapshot=_loads(row["before_snapshot"]),
        after_snapshot=_loads(row["after_snapshot"]),
        prev_hash=row["prev_hash"],
    )


def _current_entity(store: PartsStore, row: dict) -> dict | None:
    if row["entity_type"] == "part":
        return store.get_part(row["entity_key"])
    parts = row["entity_key"].split("|")
    # entity_key is canonical_number|supplier|source_ref; source_ref may
    # itself contain no pipe because the key is built with fixed positions.
    canonical_number, supplier, source_ref = parts[0], parts[1], "|".join(parts[2:])
    return store.get_quote(canonical_number, supplier, source_ref)


def verify(store: PartsStore) -> AuditReport:
    rows = store.load_ledger()
    runs = store.load_runs()
    findings: list[Finding] = []

    run_ids = {run["run_id"] for run in runs}
    run_entry_counts: dict[str, int] = {run["run_id"]: 0 for run in runs}
    run_input_hashes: dict[str, str] = {run["run_id"]: run["input_hash"] for run in runs}

    # latest seq seen per entity, used to check the "last write wins" state
    latest_seq_by_entity: dict[tuple[str, str], int] = {}
    # next entry per entity for before/after overwrite chaining
    next_before: dict[tuple[str, str], dict] = {}
    rows_by_entity: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        rows_by_entity.setdefault((row["entity_type"], row["entity_key"]), []).append(row)

    prev_hash = GENESIS_HASH
    expected_seq = 1
    first_break_seq: int | None = None

    for row in rows:
        blast = [r["seq"] for r in rows if r["seq"] >= row["seq"]]

        # 1. sequence continuity (detects missing entries and reordering)
        if row["seq"] != expected_seq:
            findings.append(
                Finding(
                    "sequence.gap",
                    row["seq"],
                    f"expected seq {expected_seq}, found {row['seq']};"
                    " entries were deleted or reordered",
                    blast,
                )
            )
            if first_break_seq is None:
                first_break_seq = row["seq"]
        expected_seq = row["seq"] + 1

        # 2. hash chain linkage and stored-hash integrity (detects tampering)
        if row["prev_hash"] != prev_hash:
            findings.append(
                Finding(
                    "chain.link_broken",
                    row["seq"],
                    "prev_hash does not match the preceding entry; chain was reordered or rewritten",
                    blast,
                )
            )
            if first_break_seq is None:
                first_break_seq = row["seq"]

        recomputed = _recompute_hash(row)
        if recomputed != row["entry_hash"]:
            findings.append(
                Finding(
                    "hash.tampered",
                    row["seq"],
                    "stored entry_hash differs from the hash recomputed from the record content",
                    blast,
                )
            )
            if first_break_seq is None:
                first_break_seq = row["seq"]
        prev_hash = row["entry_hash"]

        # 3. run binding: every entry must reference a recorded committed run,
        #    and the entry's input digest must match the run's input digest.
        if row["run_id"] not in run_ids:
            findings.append(
                Finding("run.unknown", row["seq"], f"entry references unknown run {row['run_id'][:12]}…", blast)
            )
        else:
            run_entry_counts[row["run_id"]] += 1
            if row["input_hash"] != run_input_hashes[row["run_id"]]:
                findings.append(
                    Finding(
                        "run.input_mismatch",
                        row["seq"],
                        "entry input_hash differs from the digest recorded on its import run",
                        blast,
                    )
                )

        latest_seq_by_entity[(row["entity_type"], row["entity_key"])] = row["seq"]

    # 4. before/after snapshot consistency
    for (entity_type, entity_key), entity_rows in rows_by_entity.items():
        entity_rows.sort(key=lambda r: r["seq"])
        for index, row in enumerate(entity_rows):
            blast = [r["seq"] for r in rows if r["seq"] >= row["seq"]]
            before = _loads(row["before_snapshot"])
            after = _loads(row["after_snapshot"])

            if index > 0:
                prior_after = _loads(entity_rows[index - 1]["after_snapshot"])
                if before != prior_after:
                    findings.append(
                        Finding(
                            "snapshot.discontinuity",
                            row["seq"],
                            f"{entity_type}:{entity_key} before_snapshot does not match the previous"
                            " entry's after_snapshot; history was rewritten out of band",
                            blast,
                        )
                    )
            elif before is not None:
                findings.append(
                    Finding(
                        "snapshot.unexpected_preimage",
                        row["seq"],
                        f"first ledger entry for {entity_type}:{entity_key} claims a pre-existing"
                        " state that no earlier entry produced",
                        blast,
                    )
                )

            is_latest = latest_seq_by_entity[(entity_type, entity_key)] == row["seq"]
            if is_latest:
                current = _current_entity(store, row)
                if current != after:
                    findings.append(
                        Finding(
                            "state.drift",
                            row["seq"],
                            f"current {entity_type} row does not match the last recorded"
                            " after_snapshot; business table was changed outside the pipeline",
                            blast,
                        )
                    )

    # 5. run-level completeness: recorded runs must have evidence, and no
    #    dangling evidence may exist without a run.
    for run in runs:
        if run["status"] != "committed":
            findings.append(
                Finding("run.incomplete", None, f"run {run['run_id'][:12]}… has status {run['status']}")
            )
        if run_entry_counts.get(run["run_id"], 0) == 0:
            findings.append(
                Finding(
                    "run.evidence_missing",
                    None,
                    f"run {run['run_id'][:12]}… recorded {run['record_count']} records but"
                    " no ledger entries exist",
                )
            )

    head_seq = rows[-1]["seq"] if rows else 0
    return AuditReport(
        ok=not findings,
        entries_checked=len(rows),
        runs_checked=len(runs),
        findings=findings,
        chain_head_seq=head_seq,
    )


def verify_input(store: PartsStore, raw_records: list[dict]) -> AuditReport:
    """Re-bind an external input file to the stored evidence: each raw record
    must hash to a raw_record_hash present in the ledger, in the same order.
    Returns the standard report plus input-binding findings."""
    report = verify(store)
    rows = store.load_ledger()
    by_raw_index: dict[int, list[dict]] = {}
    for row in rows:
        by_raw_index.setdefault(row["raw_index"], []).append(row)

    for index, raw in enumerate(raw_records):
        raw_hash = digest(raw)
        bound = by_raw_index.get(index, [])
        if not bound:
            report.findings.append(
                Finding("input.unrecorded", None, f"raw record at position {index} has no ledger evidence")
            )
            continue
        if any(row["raw_record_hash"] != raw_hash for row in bound):
            report.findings.append(
                Finding(
                    "input.hash_mismatch",
                    bound[0]["seq"],
                    f"raw record at position {index} does not match the hashed input stored in the ledger",
                    [r["seq"] for r in rows if r["seq"] >= bound[0]["seq"]],
                )
            )
    report.ok = not report.findings
    return report


def entity_trail(store: PartsStore, entity_type: str, entity_key: str) -> list[dict]:
    """Ordered evidence trail for one entity: every write with the rule ids,
    field-level transforms and the before/after snapshots."""
    rows = [
        row
        for row in store.load_ledger()
        if row["entity_type"] == entity_type and row["entity_key"] == entity_key
    ]
    trail = []
    for row in rows:
        trail.append(
            {
                "seq": row["seq"],
                "run_id": row["run_id"],
                "input_hash": row["input_hash"],
                "raw_index": row["raw_index"],
                "rule_set_version": row["rule_set_version"],
                "rule_ids": _loads(row["rule_ids"]),
                "transforms": _loads(row["transforms"]),
                "before": _loads(row["before_snapshot"]),
                "after": _loads(row["after_snapshot"]),
                "entry_hash": row["entry_hash"],
            }
        )
    return trail
