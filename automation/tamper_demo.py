"""Helper used by the demo script to mutate an SQLite database out of band,
simulating tampering that bypasses the pipeline. Kept separate from the
application code so the production path contains no tamper primitives."""

from __future__ import annotations
import argparse
import json
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--rewrite-ledger-seq", type=int)
    parser.add_argument("--delete-ledger-seq", type=int)
    parser.add_argument("--mutate-snapshot-seq", type=int)
    parser.add_argument("--mutate-quote-price")
    parser.add_argument("--counts", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    if args.rewrite_ledger_seq:
        # Physically reorder two adjacent rows to simulate ledger reordering.
        seq = args.rewrite_ledger_seq
        connection.execute("UPDATE evidence_ledger SET seq = -seq WHERE seq = ?", (seq,))
        connection.execute("UPDATE evidence_ledger SET seq = ? WHERE seq = ?", (seq, seq + 1))
        connection.execute("UPDATE evidence_ledger SET seq = ? WHERE seq = -?", (seq + 1, seq))
    if args.delete_ledger_seq:
        connection.execute(
            "DELETE FROM evidence_ledger WHERE seq = ?", (args.delete_ledger_seq,)
        )
    if args.mutate_snapshot_seq:
        row = connection.execute(
            "SELECT after_snapshot FROM evidence_ledger WHERE seq = ?",
            (args.mutate_snapshot_seq,),
        ).fetchone()
        snapshot = json.loads(row["after_snapshot"])
        snapshot["price"] = "0.01"
        connection.execute(
            "UPDATE evidence_ledger SET after_snapshot = ? WHERE seq = ?",
            (json.dumps(snapshot, sort_keys=True, separators=(",", ":")), args.mutate_snapshot_seq),
        )
    if args.mutate_quote_price:
        connection.execute(
            "UPDATE quotes SET price = ? WHERE canonical_number = 'AB1204'",
            (args.mutate_quote_price,),
        )
    if args.counts:
        print(
            json.dumps(
                {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("parts", "quotes", "evidence_ledger", "import_runs")
                },
                sort_keys=True,
            )
        )
    connection.commit()
    connection.close()


if __name__ == "__main__":
    main()
