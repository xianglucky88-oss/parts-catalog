from __future__ import annotations
import sqlite3
from pathlib import Path
from .models import NormalizedPart, Quote


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
            supplier_entity_id TEXT,
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        self._ensure_quote_entity_column()
        self.connection.commit()

    def _ensure_quote_entity_column(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(quotes)")}
        if "supplier_entity_id" not in columns:
            self.connection.execute("ALTER TABLE quotes ADD COLUMN supplier_entity_id TEXT")

    def save_part(self, part: NormalizedPart) -> None:
        self.connection.execute("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)", (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity))

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO quotes (canonical_number, supplier, currency, price, lead_time_days, stock_status, source_ref, supplier_entity_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                quote.canonical_number,
                quote.supplier,
                quote.currency,
                str(quote.price) if quote.price is not None else None,
                quote.lead_time_days,
                quote.stock_status,
                quote.source_ref,
                quote.supplier_entity_id,
            ),
        )

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
