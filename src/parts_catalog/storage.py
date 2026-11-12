from __future__ import annotations
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from .evidence import LEDGER_SCHEMA_VERSION, LedgerEntry, canonical_json
from .models import NormalizedPart, Quote

GENESIS_HASH = "GENESIS"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_part(part: NormalizedPart) -> dict:
    return asdict(part)


def snapshot_quote(quote: Quote) -> dict:
    data = asdict(quote)
    if data.get("price") is not None:
        data["price"] = str(data["price"])
    return data


def quote_entity_key(canonical_number: str, supplier: str, source_ref: str) -> str:
    return f"{canonical_number}|{supplier}|{source_ref}"


class PartsStore:
    """SQLite persistence for business facts plus the append-only evidence
    ledger.

    The original ``parts``/``quotes`` tables keep their exact historical
    schema and INSERT OR REPLACE semantics, so databases written by older
    versions remain fully readable. Opening such a database here only adds
    the two ledger tables; existing rows are never touched or rewritten.
    """

    def __init__(self, database: str | Path) -> None:
        # Autocommit mode: run boundaries are managed explicitly with
        # begin_run()/commit()/rollback() so business facts and their evidence
        # always share one transaction.
        self.connection = sqlite3.connect(database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""CREATE TABLE IF NOT EXISTS parts (
            canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
            unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS quotes (
            canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
            price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS import_runs (
            run_id TEXT PRIMARY KEY,
            input_hash TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            currency TEXT NOT NULL,
            rule_set_version TEXT NOT NULL,
            ledger_schema_version TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS evidence_ledger (
            seq INTEGER PRIMARY KEY,
            entry_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            raw_index INTEGER NOT NULL,
            raw_record_hash TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            rule_set_version TEXT NOT NULL,
            rule_ids TEXT NOT NULL,
            transforms TEXT NOT NULL,
            before_snapshot TEXT,
            after_snapshot TEXT,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        )""")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_entity ON evidence_ledger(entity_type, entity_key)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_run ON evidence_ledger(run_id)"
        )

    # ----- run-level transaction boundary ---------------------------------

    def begin_run(self) -> None:
        self.connection.execute("BEGIN")

    def commit(self) -> None:
        self.connection.execute("COMMIT")

    def rollback(self) -> None:
        self.connection.execute("ROLLBACK")

    # ----- historical business tables (schema/semantics unchanged) --------

    def save_part(self, part: NormalizedPart) -> None:
        self.connection.execute("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)", (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity))

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)", (quote.canonical_number, quote.supplier, quote.currency, str(quote.price) if quote.price is not None else None, quote.lead_time_days, quote.stock_status, quote.source_ref))

    def get_part(self, canonical_number: str) -> dict | None:
        row = self.connection.execute(
            "SELECT canonical_number, brand, description, unit, package_quantity FROM parts WHERE canonical_number = ?",
            (canonical_number,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_quote(self, canonical_number: str, supplier: str, source_ref: str) -> dict | None:
        row = self.connection.execute(
            "SELECT canonical_number, supplier, currency, price, lead_time_days, stock_status, source_ref"
            " FROM quotes WHERE canonical_number = ? AND supplier = ? AND source_ref = ?",
            (canonical_number, supplier, source_ref),
        ).fetchone()
        return dict(row) if row is not None else None

    # ----- import runs -----------------------------------------------------

    def run_exists(self, run_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM import_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None

    def insert_run(
        self,
        run_id: str,
        input_hash: str,
        source_ref: str,
        currency: str,
        rule_set_version: str,
        record_count: int,
        status: str,
    ) -> None:
        # Plain INSERT: a duplicated run id fails loudly instead of silently
        # overwriting an existing import record.
        self.connection.execute(
            "INSERT INTO import_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                input_hash,
                source_ref,
                currency,
                rule_set_version,
                LEDGER_SCHEMA_VERSION,
                record_count,
                status,
                _utc_now(),
            ),
        )

    def load_runs(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT run_id, input_hash, source_ref, currency, rule_set_version,"
            " ledger_schema_version, record_count, status, created_at"
            " FROM import_runs ORDER BY rowid"
        ).fetchall()
        return [dict(row) for row in rows]

    # ----- evidence ledger -------------------------------------------------

    def chain_head(self) -> tuple[int, str]:
        """Return (highest seq, its entry hash); (0, GENESIS_HASH) when empty."""
        row = self.connection.execute(
            "SELECT seq, entry_hash FROM evidence_ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return row["seq"], row["entry_hash"]

    def append_entry(self, entry: LedgerEntry) -> None:
        # Plain INSERT with seq as PRIMARY KEY: any replay or rewrite that
        # tries to reuse a sequence number is rejected by the database.
        self.connection.execute(
            "INSERT INTO evidence_ledger (seq, entry_id, run_id, input_hash, raw_index,"
            " raw_record_hash, entity_type, entity_key, rule_set_version, rule_ids,"
            " transforms, before_snapshot, after_snapshot, prev_hash, entry_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.seq,
                entry.entry_id,
                entry.run_id,
                entry.input_hash,
                entry.raw_index,
                entry.raw_record_hash,
                entry.entity_type,
                entry.entity_key,
                entry.rule_set_version,
                canonical_json(entry.rule_ids),
                canonical_json(entry.transforms),
                None if entry.before_snapshot is None else canonical_json(entry.before_snapshot),
                None if entry.after_snapshot is None else canonical_json(entry.after_snapshot),
                entry.prev_hash,
                entry.entry_hash,
            ),
        )

    def load_ledger(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT seq, entry_id, run_id, input_hash, raw_index, raw_record_hash,"
            " entity_type, entity_key, rule_set_version, rule_ids, transforms,"
            " before_snapshot, after_snapshot, prev_hash, entry_hash"
            " FROM evidence_ledger ORDER BY seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.connection.close()
