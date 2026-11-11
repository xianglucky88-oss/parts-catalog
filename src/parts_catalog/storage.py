from __future__ import annotations
import sqlite3
from pathlib import Path
from .models import BatchRecord, NormalizedPart, Quote


class PartsStore:
    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS parts (
            canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
            unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS quotes (
            canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
            price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS ingestion_batches (
            batch_id TEXT PRIMARY KEY, supplier TEXT NOT NULL, queried_at TEXT NOT NULL,
            query_part_number TEXT NOT NULL, status TEXT NOT NULL,
            received_count INTEGER NOT NULL, imported_count INTEGER NOT NULL, failed_count INTEGER NOT NULL,
            source_ref TEXT NOT NULL, ingested_at TEXT NOT NULL
        )""")
        self.connection.commit()

    def save_part(self, part: NormalizedPart) -> None:
        self.connection.execute("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)", (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity))

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)", (quote.canonical_number, quote.supplier, quote.currency, str(quote.price) if quote.price is not None else None, quote.lead_time_days, quote.stock_status, quote.source_ref))

    def batch_seen(self, batch_id: str) -> bool:
        row = self.connection.execute("SELECT 1 FROM ingestion_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        return row is not None

    def record_batch(self, record: BatchRecord) -> None:
        self.connection.execute(
            "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.batch_id, record.supplier, record.queried_at, record.query_part_number, record.status, record.received_count, record.imported_count, record.failed_count, record.source_ref, record.ingested_at),
        )

    def batch_status(self, batch_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT batch_id, supplier, status, received_count, imported_count, failed_count FROM ingestion_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        return {"batch_id": row[0], "supplier": row[1], "status": row[2], "received": row[3], "imported": row[4], "failed": row[5]}

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
