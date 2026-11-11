from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from .models import NormalizedPart, Quote

# Schema is byte-for-byte identical to the original store, so database files
# created by previous versions remain readable and writable.
_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS parts (
        canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
        unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS quotes (
        canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
        price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
        PRIMARY KEY (canonical_number, supplier, source_ref)
    )""",
)


class StorageStateError(RuntimeError):
    """Raised when a store or import batch is used outside its valid state."""


class ImportBatch:
    """One explicit import unit of work.

    * Connection owner: the batch never opens or closes the connection; it
      borrows the connection owned by :class:`PartsStore`.
    * Transaction start: ``BEGIN IMMEDIATE`` runs when the ``with`` block is
      entered — not before — so writes outside the block are rejected instead
      of silently auto-committing.
    * Commit condition: the batch commits only when the block exits without an
      exception. If ``COMMIT`` itself fails, the batch still rolls back, so a
      half-written batch can never become visible.
    * Rollback: any exception inside the block rolls the whole batch back,
      leaving the previously committed data exactly as readers last saw it.
    * After close: once the block exits — committed or rolled back — the batch
      is closed and any further write raises :class:`StorageStateError`.
    """

    def __init__(self, store: PartsStore) -> None:
        self._store = store
        self._state = "pending"
        self.parts_written = 0
        self.quotes_written = 0

    def __enter__(self) -> ImportBatch:
        if self._state != "pending":
            raise StorageStateError(f"import batch is already {self._state}")
        try:
            self._store._connection.execute("BEGIN IMMEDIATE")
        except Exception:
            self._state = "rolled-back"
            self._store._release(self)
            raise
        self._state = "active"
        return self

    def save_part(self, part: NormalizedPart) -> None:
        self._ensure_active()
        self._store._connection.execute(
            "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)",
            (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity),
        )
        self.parts_written += 1

    def save_quote(self, quote: Quote) -> None:
        self._ensure_active()
        self._store._connection.execute(
            "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                quote.canonical_number,
                quote.supplier,
                quote.currency,
                str(quote.price) if quote.price is not None else None,
                quote.lead_time_days,
                quote.stock_status,
                quote.source_ref,
            ),
        )
        self.quotes_written += 1

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._state != "active":
            # Already aborted by PartsStore.close(); nothing left to finish.
            return False
        try:
            if exc_type is None:
                try:
                    self._store._connection.execute("COMMIT")
                except sqlite3.Error:
                    # A failed commit must not strand a half-written batch.
                    try:
                        self._store._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    self._state = "rolled-back"
                    raise
                self._state = "committed"
            else:
                try:
                    self._store._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # never mask the original exception
                self._state = "rolled-back"
        finally:
            self._store._release(self)
        return False  # never suppress the original exception

    @property
    def committed(self) -> bool:
        return self._state == "committed"

    @property
    def rolled_back(self) -> bool:
        return self._state == "rolled-back"

    def _abort(self) -> None:
        """Roll back an unfinished batch; called when the owning store closes."""
        if self._state == "active":
            try:
                self._store._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        if self._state in ("pending", "active"):
            self._state = "rolled-back"
        self._store._release(self)

    def _ensure_active(self) -> None:
        if self._state == "pending":
            raise StorageStateError("import batch has not begun; use it as a context manager")
        if self._state != "active":
            raise StorageStateError(f"import batch is closed ({self._state}); start a new batch")


class PartsStore:
    """Owns the SQLite connection to the catalog database.

    The store is the single connection owner: it opens the database, applies
    the schema (unchanged, so legacy database files remain readable), and
    closes the connection. Callers cannot write or commit through the store
    itself — the only write path is :meth:`import_batch`, which publishes the
    whole batch or nothing at all.
    """

    _batch_class = ImportBatch

    def __init__(self, database: str | Path) -> None:
        # isolation_level=None puts the driver in autocommit mode: schema DDL
        # applies immediately, and every import batch controls its transaction
        # boundary with explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK statements.
        self._connection = sqlite3.connect(database, isolation_level=None)
        for statement in _SCHEMA_STATEMENTS:
            self._connection.execute(statement)
        self._closed = False
        self._active_batch: ImportBatch | None = None

    def import_batch(self) -> ImportBatch:
        """Create the unit of work for one import run (at most one may be active)."""
        self._ensure_open()
        if self._active_batch is not None:
            raise StorageStateError("another import batch is still active on this store")
        batch = self._batch_class(self)
        self._active_batch = batch
        return batch

    def read_parts(self) -> list[NormalizedPart]:
        """Return the last committed catalog rows — what readers can rely on."""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT canonical_number, brand, description, unit, package_quantity"
            " FROM parts ORDER BY canonical_number"
        ).fetchall()
        return [
            NormalizedPart(
                canonical_number=row[0],
                brand=row[1],
                description=row[2],
                unit=row[3],
                package_quantity=row[4],
            )
            for row in rows
        ]

    def read_quotes(self) -> list[Quote]:
        """Return the last committed quote rows — what readers can rely on."""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT canonical_number, supplier, currency, price, lead_time_days,"
            " stock_status, source_ref FROM quotes"
            " ORDER BY canonical_number, supplier, source_ref"
        ).fetchall()
        return [
            Quote(
                canonical_number=row[0],
                supplier=row[1],
                currency=row[2],
                price=Decimal(row[3]) if row[3] is not None else None,
                lead_time_days=row[4],
                stock_status=row[5],
                source_ref=row[6],
            )
            for row in rows
        ]

    def close(self) -> None:
        """Close the connection. Idempotent; an unfinished batch is rolled back."""
        if self._closed:
            return
        try:
            if self._active_batch is not None:
                self._active_batch._abort()
        finally:
            self._connection.close()
            self._closed = True

    def _release(self, batch: ImportBatch) -> None:
        if self._active_batch is batch:
            self._active_batch = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageStateError("parts store is closed")
