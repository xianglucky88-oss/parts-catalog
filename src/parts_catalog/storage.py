from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from .errors import (
    IncompleteRunError,
    OutOfScopeError,
    RunAlreadyPublishedError,
    RunStateError,
)
from .models import NormalizedPart, Quote, RunInfo, ScopeEntry, VisibleQuote


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value) -> str:
    return _to_datetime(value).isoformat(timespec="seconds")


class PartsStore:
    """SQLite persistence implementing the publication semantics of the
    supplier activity view:

    * a collection run pins its query scope at begin time and may only ever
      affect those (supplier, canonical_number) cells;
    * deliveries land in staged_quotes — durable for safe retry, but never
      visible to procurement;
    * the published view (quotes) is replaced cell-by-cell inside a single
      transaction, and only when the run covers its whole pinned scope;
    * incomplete or failed runs leave the previous complete publication
      untouched, so procurement keeps trusting the last full result.
    """

    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        conn = self.connection
        conn.execute("""CREATE TABLE IF NOT EXISTS parts (
            canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
            unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
        )""")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "quotes" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
            if "run_id" not in columns:
                # Pre-run-schema databases: keep the old direct-write table for
                # inspection, but it no longer feeds the visible view.
                conn.execute("ALTER TABLE quotes RENAME TO quotes_legacy_v1")
        conn.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
            run_id TEXT PRIMARY KEY,
            source_ref TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('collecting', 'published', 'failed')),
            expected_queries INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            valid_until TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS run_scope (
            run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
            supplier TEXT NOT NULL,
            canonical_number TEXT NOT NULL,
            PRIMARY KEY (run_id, supplier, canonical_number)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS staged_quotes (
            run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
            supplier TEXT NOT NULL,
            canonical_number TEXT NOT NULL,
            currency TEXT NOT NULL,
            price TEXT,
            lead_time_days INTEGER,
            stock_status TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (run_id, supplier, canonical_number)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quotes (
            supplier TEXT NOT NULL,
            canonical_number TEXT NOT NULL,
            currency TEXT NOT NULL,
            price TEXT,
            lead_time_days INTEGER,
            stock_status TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
            source_ref TEXT NOT NULL,
            published_at TEXT NOT NULL,
            valid_until TEXT,
            PRIMARY KEY (supplier, canonical_number)
        )""")
        conn.commit()

    # -- run lifecycle -----------------------------------------------------

    def begin_run(self, run_id: str, source_ref: str, scope, started_at=None, valid_until=None) -> RunInfo:
        """Pin the query scope of a collection run.

        Re-beginning an unfinished run with the identical scope and source_ref
        is a safe no-op (a failed run resumes as collecting); a conflicting
        scope or an already-published run_id is rejected.
        """
        entries = [ScopeEntry(e.supplier, e.canonical_number) for e in scope]
        if not entries:
            raise RunStateError("run scope must pin at least one query")
        if len({(e.supplier, e.canonical_number) for e in entries}) != len(entries):
            raise RunStateError("run scope contains duplicate queries")
        existing = self._run_row(run_id)
        if existing is None:
            started = _iso(started_at) if started_at is not None else _iso(_utcnow())
            expiry = _iso(valid_until) if valid_until is not None else None
            try:
                with self.connection:
                    self.connection.execute(
                        "INSERT INTO collection_runs VALUES (?, ?, 'collecting', ?, ?, NULL, ?)",
                        (run_id, source_ref, len(entries), started, expiry),
                    )
                    self.connection.executemany(
                        "INSERT INTO run_scope VALUES (?, ?, ?)",
                        [(run_id, e.supplier, e.canonical_number) for e in entries],
                    )
            except sqlite3.IntegrityError as exc:
                raise RunStateError(f"run {run_id!r} already exists") from exc
            return self.run_info(run_id)
        pinned = {(e.supplier, e.canonical_number) for e in self._scope_entries(run_id)}
        if existing["source_ref"] != source_ref or pinned != {(e.supplier, e.canonical_number) for e in entries}:
            raise RunStateError(f"run {run_id!r} already exists with a different scope or source_ref")
        if existing["status"] == "published":
            raise RunAlreadyPublishedError(f"run {run_id!r} is already published; start a new run_id")
        if existing["status"] == "failed":
            with self.connection:
                self.connection.execute(
                    "UPDATE collection_runs SET status = 'collecting', finished_at = NULL WHERE run_id = ?",
                    (run_id,),
                )
        return self.run_info(run_id)

    def stage_delivery(self, run_id: str, parts, quotes, delivered_at=None) -> int:
        """Stage one validated delivery batch atomically.

        Repeated deliveries of the same cell overwrite the staged row, so
        retransmission never duplicates. Every cell must belong to the run's
        pinned scope. Returns the number of distinct cells staged.
        """
        run = self._run_row(run_id)
        if run is None:
            raise RunStateError(f"unknown run {run_id!r}; begin the run and pin its scope first")
        if run["status"] == "published":
            raise RunStateError(f"run {run_id!r} is already published; deliveries are immutable")
        pinned = {(e.supplier, e.canonical_number) for e in self._scope_entries(run_id)}
        offenders = sorted({(q.supplier, q.canonical_number) for q in quotes} - pinned)
        if offenders:
            listing = ", ".join(f"{supplier}/{number}" for supplier, number in offenders)
            raise OutOfScopeError(f"delivered cells outside the pinned scope of run {run_id!r}: {listing}")
        stamped = _iso(delivered_at) if delivered_at is not None else _iso(_utcnow())
        with self.connection:
            self.connection.executemany(
                "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)",
                [(p.canonical_number, p.brand, p.description, p.unit, p.package_quantity) for p in parts],
            )
            self.connection.executemany(
                "INSERT OR REPLACE INTO staged_quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        q.supplier,
                        q.canonical_number,
                        q.currency,
                        str(q.price) if q.price is not None else None,
                        q.lead_time_days,
                        q.stock_status,
                        stamped,
                    )
                    for q in quotes
                ],
            )
            if run["status"] == "failed":
                self.connection.execute(
                    "UPDATE collection_runs SET status = 'collecting', finished_at = NULL WHERE run_id = ?",
                    (run_id,),
                )
        return len({(q.supplier, q.canonical_number) for q in quotes})

    def complete_run(self, run_id: str, finished_at=None) -> str:
        """Publish a run atomically once its pinned scope is fully answered.

        Only the run's scope cells are replaced in the visible view; every
        other cell keeps its previous publication. Raises IncompleteRunError
        while any pinned query is still unanswered, leaving the previous
        complete publication visible.
        """
        run = self._run_row(run_id)
        if run is None:
            raise RunStateError(f"unknown run {run_id!r}")
        if run["status"] == "published":
            return "already_published"
        pending = self.pending_queries(run_id)
        if pending:
            raise IncompleteRunError(run_id, pending)
        finished = _iso(finished_at) if finished_at is not None else _iso(_utcnow())
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO quotes
                   (supplier, canonical_number, currency, price, lead_time_days,
                    stock_status, run_id, source_ref, published_at, valid_until)
                   SELECT s.supplier, s.canonical_number, s.currency, s.price,
                          s.lead_time_days, s.stock_status, r.run_id, r.source_ref, ?, r.valid_until
                   FROM staged_quotes s
                   JOIN collection_runs r ON r.run_id = s.run_id
                   WHERE s.run_id = ?""",
                (finished, run_id),
            )
            self.connection.execute(
                "UPDATE collection_runs SET status = 'published', finished_at = ? WHERE run_id = ?",
                (finished, run_id),
            )
        return "published"

    def fail_run(self, run_id: str, finished_at=None) -> None:
        """Mark a run failed without touching the visible view. Staged
        deliveries are kept so the run can be resumed and retried safely."""
        run = self._run_row(run_id)
        if run is None:
            raise RunStateError(f"unknown run {run_id!r}")
        if run["status"] == "published":
            raise RunStateError(f"run {run_id!r} is already published")
        finished = _iso(finished_at) if finished_at is not None else _iso(_utcnow())
        with self.connection:
            self.connection.execute(
                "UPDATE collection_runs SET status = 'failed', finished_at = ? WHERE run_id = ?",
                (finished, run_id),
            )

    # -- inspection --------------------------------------------------------

    def run_info(self, run_id: str) -> RunInfo:
        run = self._run_row(run_id)
        if run is None:
            raise RunStateError(f"unknown run {run_id!r}")
        staged = self.connection.execute(
            "SELECT COUNT(*) FROM staged_quotes WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        return RunInfo(
            run_id=run["run_id"],
            source_ref=run["source_ref"],
            status=run["status"],
            expected_queries=run["expected_queries"],
            staged_queries=staged,
            started_at=run["started_at"],
            finished_at=run["finished_at"],
            valid_until=run["valid_until"],
        )

    def pending_queries(self, run_id: str) -> list[ScopeEntry]:
        """Pinned queries that still have no staged answer — exactly the work
        a retry must redo. An answer may be a quote or an explicit no-quote;
        a missing row means the query never returned."""
        if self._run_row(run_id) is None:
            raise RunStateError(f"unknown run {run_id!r}")
        rows = self.connection.execute(
            """SELECT sc.supplier, sc.canonical_number
               FROM run_scope sc
               LEFT JOIN staged_quotes sq
                 ON sq.run_id = sc.run_id
                AND sq.supplier = sc.supplier
                AND sq.canonical_number = sc.canonical_number
               WHERE sc.run_id = ? AND sq.run_id IS NULL
               ORDER BY sc.supplier, sc.canonical_number""",
            (run_id,),
        ).fetchall()
        return [ScopeEntry(row["supplier"], row["canonical_number"]) for row in rows]

    def visible_quotes(self, now=None) -> list[VisibleQuote]:
        """The procurement-visible view: exactly one row per published cell,
        each explicitly marked "current" or "expired"."""
        moment = _to_datetime(now) if now is not None else _utcnow()
        rows = self.connection.execute(
            """SELECT supplier, canonical_number, currency, price, lead_time_days,
                      stock_status, run_id, source_ref, published_at, valid_until
               FROM quotes ORDER BY supplier, canonical_number"""
        ).fetchall()
        view = []
        for row in rows:
            expiry = _to_datetime(row["valid_until"]) if row["valid_until"] else None
            visibility = "current" if expiry is None or moment < expiry else "expired"
            view.append(
                VisibleQuote(
                    supplier=row["supplier"],
                    canonical_number=row["canonical_number"],
                    currency=row["currency"],
                    price=Decimal(row["price"]) if row["price"] is not None else None,
                    lead_time_days=row["lead_time_days"],
                    stock_status=row["stock_status"],
                    run_id=row["run_id"],
                    source_ref=row["source_ref"],
                    published_at=row["published_at"],
                    valid_until=row["valid_until"],
                    visibility=visibility,
                )
            )
        return view

    def expire_source(self, source_ref: str, expired_at=None) -> int:
        """Deterministically expire every published quote from a source. Rows
        stay visible but are marked expired — never silently current, never
        silently gone. Returns the number of rows marked."""
        stamp = _iso(expired_at) if expired_at is not None else _iso(_utcnow())
        moment = _to_datetime(stamp)
        rows = self.connection.execute(
            "SELECT rowid, valid_until FROM quotes WHERE source_ref = ?", (source_ref,)
        ).fetchall()
        targets = [
            row["rowid"]
            for row in rows
            if row["valid_until"] is None or _to_datetime(row["valid_until"]) > moment
        ]
        with self.connection:
            self.connection.executemany(
                "UPDATE quotes SET valid_until = ? WHERE rowid = ?",
                [(stamp, rowid) for rowid in targets],
            )
        return len(targets)

    def close(self) -> None:
        self.connection.close()

    # -- internals ---------------------------------------------------------

    def _run_row(self, run_id: str):
        return self.connection.execute(
            "SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def _scope_entries(self, run_id: str) -> list[ScopeEntry]:
        rows = self.connection.execute(
            "SELECT supplier, canonical_number FROM run_scope WHERE run_id = ? ORDER BY supplier, canonical_number",
            (run_id,),
        ).fetchall()
        return [ScopeEntry(row["supplier"], row["canonical_number"]) for row in rows]
