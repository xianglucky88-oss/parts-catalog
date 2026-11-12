from __future__ import annotations
import json
import sqlite3
from pathlib import Path

from .models import NormalizedPart, Quote
from .quality import QualityDecision, SupplierQualityProfile


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
        # Quality-gate outputs.  The fingerprint primary key makes replaying
        # identical inputs an upsert, so no duplicate quarantine rows appear.
        self.connection.execute("""CREATE TABLE IF NOT EXISTS quality_decisions (
            fingerprint TEXT PRIMARY KEY, policy_version TEXT NOT NULL,
            supplier TEXT NOT NULL, part_number TEXT NOT NULL, verdict TEXT NOT NULL,
            coverage_ratio REAL NOT NULL, reasons TEXT NOT NULL, raw_record TEXT NOT NULL,
            source_ref TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS quality_profiles (
            supplier TEXT NOT NULL, policy_version TEXT NOT NULL, source_ref TEXT NOT NULL,
            total INTEGER NOT NULL, accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL,
            rejected INTEGER NOT NULL, average_coverage REAL NOT NULL, detail TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (supplier, policy_version, source_ref)
        )""")
        self.connection.commit()

    def save_part(self, part: NormalizedPart) -> None:
        self.connection.execute("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)", (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity))

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)", (quote.canonical_number, quote.supplier, quote.currency, str(quote.price) if quote.price is not None else None, quote.lead_time_days, quote.stock_status, quote.source_ref))

    def save_quality_decision(self, decision: QualityDecision, source_ref: str) -> None:
        """Persist a non-accepted verdict (rejected/quarantined) idempotently."""
        self.connection.execute(
            """INSERT INTO quality_decisions
               (fingerprint, policy_version, supplier, part_number, verdict,
                coverage_ratio, reasons, raw_record, source_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET
                 policy_version=excluded.policy_version, supplier=excluded.supplier,
                 part_number=excluded.part_number, verdict=excluded.verdict,
                 coverage_ratio=excluded.coverage_ratio, reasons=excluded.reasons,
                 raw_record=excluded.raw_record, source_ref=excluded.source_ref""",
            (
                decision.fingerprint,
                decision.policy_version,
                decision.supplier,
                decision.part_number,
                decision.verdict,
                decision.coverage_ratio,
                json.dumps(decision.reason_dicts(), ensure_ascii=False, default=str),
                json.dumps(decision.raw, ensure_ascii=False, default=str),
                source_ref,
            ),
        )

    def save_quality_profile(self, profile: SupplierQualityProfile, source_ref: str) -> None:
        """Upsert the per-supplier quality profile so replays stay consistent."""
        self.connection.execute(
            """INSERT INTO quality_profiles
               (supplier, policy_version, source_ref, total, accepted, quarantined,
                rejected, average_coverage, detail, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(supplier, policy_version, source_ref) DO UPDATE SET
                 total=excluded.total, accepted=excluded.accepted,
                 quarantined=excluded.quarantined, rejected=excluded.rejected,
                 average_coverage=excluded.average_coverage, detail=excluded.detail,
                 updated_at=datetime('now')""",
            (
                profile.supplier,
                profile.policy_version,
                source_ref,
                profile.total,
                profile.accepted,
                profile.quarantined,
                profile.rejected,
                profile.average_coverage,
                json.dumps(profile.as_dict(), ensure_ascii=False, default=str),
            ),
        )

    # -- read helpers (verification / introspection) -----------------------
    def count(self, table: str) -> int:
        if table not in {"parts", "quotes", "quality_decisions", "quality_profiles"}:
            raise ValueError(f"unknown table {table}")
        row = self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def quality_decisions(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT fingerprint, policy_version, supplier, part_number, verdict, "
            "coverage_ratio, reasons, raw_record, source_ref FROM quality_decisions ORDER BY fingerprint"
        ).fetchall()
        return [
            {
                "fingerprint": r[0],
                "policy_version": r[1],
                "supplier": r[2],
                "part_number": r[3],
                "verdict": r[4],
                "coverage_ratio": r[5],
                "reasons": json.loads(r[6]),
                "raw_record": json.loads(r[7]),
                "source_ref": r[8],
            }
            for r in rows
        ]

    def quality_profiles(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT supplier, policy_version, source_ref, total, accepted, quarantined, "
            "rejected, average_coverage, detail FROM quality_profiles ORDER BY supplier"
        ).fetchall()
        return [
            {
                "supplier": r[0],
                "policy_version": r[1],
                "source_ref": r[2],
                "total": r[3],
                "accepted": r[4],
                "quarantined": r[5],
                "rejected": r[6],
                "average_coverage": r[7],
                "detail": json.loads(r[8]),
            }
            for r in rows
        ]

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
