#!/usr/bin/env bash
# End-to-end demonstration of the field-level evidence ledger.
#
# Scenarios:
#   1. normal import   - business facts + hash-chained evidence in one txn
#   2. out-of-order    - shuffled input is a distinct, still-verifiable run
#   3. replay          - identical input adds zero facts and zero evidence
#   4. tampering       - snapshot mutation, row deletion, reorder and business
#                        table drift are all located with blast radius
#   5. interrupted     - fault before commit leaves nothing behind
#
# Usage: PYTHONPATH=src bash automation/demo_evidence.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DB="$WORK/catalog.sqlite3"
CLI=(python3 -m parts_catalog.cli)
TAMPER=(python3 automation/tamper_demo.py "$DB")
FIX="fixtures/supplier_payload.json"
SHUFFLED="fixtures/supplier_payload_shuffled.json"

section() { printf '\n========== %s ==========\n' "$1"; }

section "1. normal import (facts + evidence, single transaction)"
"${CLI[@]}" import --fixture "$FIX" --database "$DB"
"${CLI[@]}" audit --database "$DB" --fixture "$FIX"
echo "-- evidence trail of the AB1204 quote (both writes, before/after) --"
"${CLI[@]}" trail --database "$DB" --type quote \
  --canonical AB1204 --supplier Northstar --source-ref "$FIX" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
for e in d["trail"]:
    print(json.dumps({
        "seq": e["seq"],
        "raw_index": e["raw_index"],
        "rules": e["rule_ids"],
        "before_price": (e["before"] or {}).get("price"),
        "after_price": e["after"]["price"],
    }, sort_keys=True))
'

section "2. out-of-order input (records arrive in reverse order)"
"${CLI[@]}" import --fixture "$SHUFFLED" --database "$DB"
echo "-- audit still ok; note the distinct input_hash/run_id above --"
"${CLI[@]}" audit --database "$DB"

section "3. replay of the identical import"
"${CLI[@]}" audit --database "$DB" >/dev/null  # baseline
BEFORE_ROWS="$("${TAMPER[@]}" --counts)"
"${CLI[@]}" import --fixture "$FIX" --database "$DB"
AFTER_ROWS="$("${TAMPER[@]}" --counts)"
echo "row counts before replay: $BEFORE_ROWS"
echo "row counts after  replay: $AFTER_ROWS"
[ "$BEFORE_ROWS" = "$AFTER_ROWS" ] && echo "OK: replay added no business facts and no evidence"
# Snapshot the verified-clean database; every tamper scenario starts from it.
cp "$DB" "$WORK/clean.sqlite3"

section "4a. tamper: rewrite a stored after_snapshot outside the pipeline"
cp "$WORK/clean.sqlite3" "$DB"
"${TAMPER[@]}" --mutate-snapshot-seq 2
"${CLI[@]}" audit --database "$DB"; echo "audit exit=$? (2 = findings)"

section "4b. tamper: delete an evidence entry (missing evidence)"
cp "$WORK/clean.sqlite3" "$DB"
"${TAMPER[@]}" --delete-ledger-seq 3
"${CLI[@]}" audit --database "$DB"; echo "audit exit=$?"

section "4c. tamper: physically reorder ledger rows"
cp "$WORK/clean.sqlite3" "$DB"
"${TAMPER[@]}" --rewrite-ledger-seq 2
"${CLI[@]}" audit --database "$DB"; echo "audit exit=$?"

section "4d. tamper: change the business table without evidence"
cp "$WORK/clean.sqlite3" "$DB"
"${TAMPER[@]}" --mutate-quote-price 0.01
"${CLI[@]}" audit --database "$DB"; echo "audit exit=$?"

section "5. interrupted write (fault injected immediately before commit)"
FAULT_DB="$WORK/fault.sqlite3"
"${CLI[@]}" import --fixture "$FIX" --database "$FAULT_DB" --simulate-fault
echo "exit=$? (4 = rolled back)"
python3 automation/tamper_demo.py "$FAULT_DB" --counts
echo "-- the same import then succeeds cleanly --"
"${CLI[@]}" import --fixture "$FIX" --database "$FAULT_DB"
"${CLI[@]}" audit --database "$FAULT_DB"

section "done"
echo "all databases were temporary; nothing was written under the repository"
