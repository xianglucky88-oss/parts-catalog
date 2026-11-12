from __future__ import annotations
import sqlite3
from decimal import Decimal
from pathlib import Path
from .models import NormalizedPart, Quote
from .unit_cost import (
    Dimension,
    MeasurementBasis,
    UnitCostResult,
    UnitCostStatus,
    IncomparabilityReason,
)


class PartsStore:
    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS parts (
            canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
            unit TEXT NOT NULL, package_quantity INTEGER
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS quotes (
            canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
            price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'EA', package_quantity INTEGER,
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS unit_costs (
            canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, source_ref TEXT NOT NULL,
            currency TEXT NOT NULL, unit TEXT NOT NULL, package_quantity INTEGER,
            dimension TEXT NOT NULL, measurement_basis TEXT, status TEXT NOT NULL, reason TEXT,
            unit_cost TEXT, conversion_factor TEXT, conversion_basis TEXT,
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        """Upgrade databases created by earlier snapshots in place."""
        part_cols = {row[1]: row for row in self.connection.execute("PRAGMA table_info(parts)")}
        # Earlier schema forced package_quantity to NOT NULL; rebuild so an
        # unknown packaging dimension can be stored honestly as NULL.
        if part_cols and part_cols["package_quantity"][3] == 1:
            self.connection.executescript(
                """
                CREATE TABLE parts_new (
                    canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
                    unit TEXT NOT NULL, package_quantity INTEGER
                );
                INSERT INTO parts_new SELECT canonical_number, brand, description, unit, package_quantity FROM parts;
                DROP TABLE parts;
                ALTER TABLE parts_new RENAME TO parts;
                """
            )
        quote_cols = {row[1] for row in self.connection.execute("PRAGMA table_info(quotes)")}
        if "unit" not in quote_cols:
            self.connection.execute("ALTER TABLE quotes ADD COLUMN unit TEXT NOT NULL DEFAULT 'EA'")
        if "package_quantity" not in quote_cols:
            self.connection.execute("ALTER TABLE quotes ADD COLUMN package_quantity INTEGER")

    def save_part(self, part: NormalizedPart) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)",
            (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity),
        )

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                quote.canonical_number, quote.supplier, quote.currency,
                str(quote.price) if quote.price is not None else None,
                quote.lead_time_days, quote.stock_status, quote.source_ref,
                quote.unit, quote.package_quantity,
            ),
        )

    def save_unit_cost(self, result: UnitCostResult, source_ref: str) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO unit_costs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.canonical_number, result.supplier, source_ref, result.currency,
                result.unit, result.package_quantity, result.dimension.value,
                result.measurement_basis.value if result.measurement_basis is not None else None,
                result.status.value,
                result.reason.value if result.reason is not None else None,
                str(result.unit_cost) if result.unit_cost is not None else None,
                str(result.conversion_factor) if result.conversion_factor is not None else None,
                result.conversion_basis,
            ),
        )

    def fetch_unit_costs(self, canonical_number: str) -> list[UnitCostResult]:
        rows = self.connection.execute(
            "SELECT * FROM unit_costs WHERE canonical_number = ? ORDER BY supplier, source_ref",
            (canonical_number,),
        ).fetchall()
        return [
            UnitCostResult(
                supplier=row[1],
                canonical_number=row[0],
                currency=row[3],
                unit=row[4],
                package_quantity=row[5],
                dimension=Dimension(row[6]),
                measurement_basis=MeasurementBasis(row[7]) if row[7] is not None else None,
                status=UnitCostStatus(row[8]),
                reason=IncomparabilityReason(row[9]) if row[9] is not None else None,
                unit_cost=Decimal(row[10]) if row[10] is not None else None,
                conversion_factor=Decimal(row[11]) if row[11] is not None else None,
                conversion_basis=row[12],
            )
            for row in rows
        ]

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
