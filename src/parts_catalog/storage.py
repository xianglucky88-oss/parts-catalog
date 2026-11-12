from __future__ import annotations
import sqlite3
from pathlib import Path
from .merge import MISSING, Decision, FieldFact, Incumbent, decide
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
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        # Current attribution of every managed catalog field: who owns the
        # value now, from which source, with the sign-off note if any.
        self.connection.execute("""CREATE TABLE IF NOT EXISTS field_provenance (
            canonical_number TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
            origin TEXT NOT NULL, source_ref TEXT NOT NULL, note TEXT,
            PRIMARY KEY (canonical_number, field)
        )""")
        # Reviewable adjudication ledger: the first verdict for each distinct
        # incoming assertion (part, field, incoming value, incoming origin).
        # INSERT OR IGNORE keeps replays idempotent, so re-import never drifts.
        self.connection.execute("""CREATE TABLE IF NOT EXISTS field_decisions (
            canonical_number TEXT NOT NULL, field TEXT NOT NULL,
            incoming_value TEXT NOT NULL, incoming_origin TEXT NOT NULL,
            outcome TEXT NOT NULL, reason TEXT NOT NULL,
            incumbent_value TEXT, incumbent_origin TEXT,
            source_ref TEXT NOT NULL, note TEXT,
            PRIMARY KEY (canonical_number, field, incoming_value, incoming_origin)
        )""")
        self.connection.commit()

    def load_provenance(self, canonical_number: str) -> dict[str, Incumbent]:
        rows = self.connection.execute(
            "SELECT field, value, origin, source_ref, note FROM field_provenance WHERE canonical_number = ?",
            (canonical_number,))
        return {field: Incumbent(value, origin, source_ref, note)
                for field, value, origin, source_ref, note in rows}

    def merge_part(self, part: NormalizedPart, facts: list[FieldFact]) -> list[Decision]:
        """Adjudicate each incoming field fact against current provenance,
        persist every decision, and materialize the parts row from the
        winning values. Quote facts are handled separately and never touch
        this path."""
        incumbents = self.load_provenance(part.canonical_number)
        decisions: list[Decision] = []
        for fact in facts:
            decision = decide(incumbents.get(fact.field), fact)
            decisions.append(decision)
            if decision.won:
                value = str(fact.value)
                self.connection.execute(
                    "INSERT OR REPLACE INTO field_provenance VALUES (?, ?, ?, ?, ?, ?)",
                    (part.canonical_number, fact.field, value, fact.origin, fact.source_ref, fact.note))
                incumbents[fact.field] = Incumbent(value, fact.origin, fact.source_ref, fact.note)
            self.connection.execute(
                "INSERT OR IGNORE INTO field_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (part.canonical_number, fact.field,
                 str(fact.value) if fact.value is not None else MISSING, fact.origin,
                 decision.outcome, decision.reason,
                 decision.incumbent.value if decision.incumbent else None,
                 decision.incumbent.origin if decision.incumbent else None,
                 fact.source_ref, fact.note))
        self._materialize_part(part)
        return decisions

    def _materialize_part(self, part: NormalizedPart) -> None:
        """Rebuild the parts row from provenance. Fields never asserted by
        any source keep the normalized contract defaults and simply have no
        provenance row, which is how reviewers see they are unowned."""
        provenance = self.load_provenance(part.canonical_number)

        def value(field: str, default: object) -> object:
            incumbent = provenance.get(field)
            return incumbent.value if incumbent else default

        self.connection.execute(
            "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)",
            (part.canonical_number,
             str(value("brand", part.brand)),
             str(value("description", part.description)),
             str(value("unit", part.unit)),
             int(str(value("package_quantity", part.package_quantity)))))

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)", (quote.canonical_number, quote.supplier, quote.currency, str(quote.price) if quote.price is not None else None, quote.lead_time_days, quote.stock_status, quote.source_ref))

    def review(self, canonical_number: str) -> dict:
        """Everything a review meeting needs for one catalog part: current
        row, per-field attribution, the adjudication ledger, and the quotes
        that were kept independent of catalog adjudication."""
        part_row = self.connection.execute(
            "SELECT canonical_number, brand, description, unit, package_quantity FROM parts WHERE canonical_number = ?",
            (canonical_number,)).fetchone()
        provenance = [dict(zip(("field", "value", "origin", "source_ref", "note"), row))
                      for row in self.connection.execute(
                          "SELECT field, value, origin, source_ref, note FROM field_provenance "
                          "WHERE canonical_number = ? ORDER BY field", (canonical_number,))]
        decisions = [dict(zip(("field", "incoming_value", "incoming_origin", "outcome", "reason",
                               "incumbent_value", "incumbent_origin", "source_ref", "note"), row))
                     for row in self.connection.execute(
                         "SELECT field, incoming_value, incoming_origin, outcome, reason, "
                         "incumbent_value, incumbent_origin, source_ref, note FROM field_decisions "
                         "WHERE canonical_number = ? ORDER BY field, incoming_origin, incoming_value",
                         (canonical_number,))]
        quotes = [dict(zip(("supplier", "currency", "price", "lead_time_days", "stock_status", "source_ref"), row))
                  for row in self.connection.execute(
                      "SELECT supplier, currency, price, lead_time_days, stock_status, source_ref "
                      "FROM quotes WHERE canonical_number = ? ORDER BY supplier, source_ref",
                      (canonical_number,))]
        return {
            "part": dict(zip(("canonical_number", "brand", "description", "unit", "package_quantity"), part_row)) if part_row else None,
            "provenance": provenance,
            "decisions": decisions,
            "quotes": quotes,
        }

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
