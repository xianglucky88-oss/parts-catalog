"""Cross-layer data-engineering contracts: fixture -> normalize -> pipeline -> SQLite.

These tests run fully offline against a temporary SQLite database and use the
real JsonFixtureSource / PartsPipeline / PartsStore wiring, so they verify that
collected supplier records actually arrive in the catalog and quote stores.
Runnable with the stdlib only:

    PYTHONPATH=src python3 -m unittest discover -s tests -v

pytest also collects unittest.TestCase classes, so `pytest` works unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "fixtures" / "supplier_payload.json"


class PipelineTestCase(unittest.TestCase):
    """Runs the real pipeline into a throwaway SQLite file per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog.sqlite3"
        self.store = PartsStore(self.db_path)
        self.addCleanup(self.store.close)

    def run_pipeline(self, source, **kwargs) -> int:
        return PartsPipeline(source, self.store).run(**kwargs)

    def write_payload(self, payload) -> Path:
        path = Path(self.tmp.name) / "payload.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        # A fresh connection only ever sees committed data, which is exactly
        # what downstream consumers of the database file would observe.
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()


class TestFixtureToSQLiteContract(PipelineTestCase):
    """The repository fixture must land in both stores with normalized keys."""

    def test_fixture_records_reach_parts_table_with_canonical_key(self):
        processed = self.run_pipeline(JsonFixtureSource(FIXTURE))
        self.assertEqual(processed, 2)
        parts = self.query("SELECT canonical_number, brand, unit, package_quantity FROM parts")
        self.assertEqual(parts, [("AB1204", "ACME", "EA", 1)])

    def test_every_processed_record_persists_a_quote(self):
        processed = self.run_pipeline(JsonFixtureSource(FIXTURE))
        quotes = self.query("SELECT canonical_number, supplier, source_ref FROM quotes")
        self.assertEqual(
            len(quotes),
            processed,
            "pipeline reported records processed that are not retrievable as quotes",
        )

    def test_distinct_supplier_offers_are_not_silently_overwritten(self):
        self.run_pipeline(JsonFixtureSource(FIXTURE))
        offers = self.query(
            "SELECT price, lead_time_days FROM quotes WHERE canonical_number = ? ORDER BY price",
            ("AB1204",),
        )
        self.assertEqual(offers, [("18.50", 4), ("18.75", 5)])

    def test_quote_row_matches_normalized_contract(self):
        self.run_pipeline(JsonFixtureSource(FIXTURE), currency="usd", source_ref="regression")
        rows = self.query(
            "SELECT canonical_number, supplier, currency, price, lead_time_days, stock_status, source_ref FROM quotes"
        )
        self.assertEqual(len(rows), 1)
        canonical, supplier, currency, price, lead_time, status, source_ref = rows[0]
        self.assertEqual(canonical, "AB1204")
        self.assertEqual(supplier, "Northstar")
        self.assertEqual(currency, "USD", "currency must be upper-cased before persistence")
        self.assertEqual(Decimal(price), Decimal("18.75"), "price TEXT must round-trip to Decimal")
        self.assertEqual(lead_time, 5)
        self.assertEqual(status, "available", "stock status must be alias-normalized")
        self.assertEqual(source_ref, "regression")

    def test_parts_and_quotes_join_on_canonical_number(self):
        self.run_pipeline(JsonFixtureSource(FIXTURE))
        rows = self.query(
            "SELECT p.canonical_number, p.brand, q.supplier "
            "FROM parts p JOIN quotes q ON q.canonical_number = p.canonical_number"
        )
        self.assertTrue(rows, "catalog parts and stored quotes must join on canonical_number")
        for _, brand, supplier in rows:
            self.assertEqual(brand, "ACME")
            self.assertEqual(supplier, "Northstar")


class TestPipelineBoundaries(PipelineTestCase):
    """Empty, invalid and repeated inputs must behave predictably at the store layer."""

    def test_empty_payload_persists_schema_but_no_rows(self):
        processed = self.run_pipeline(JsonFixtureSource(self.write_payload([])))
        self.assertEqual(processed, 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM parts"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM quotes"), [(0,)])

    def test_invalid_record_aborts_whole_batch_atomically(self):
        payload = [
            {"supplier": "Northstar", "part_number": "ZZ-1", "brand": "Acme", "price": "9.99"},
            {"supplier": "Northstar", "part_number": "ZZ-2", "brand": "Acme", "price": "not-a-price"},
        ]
        with self.assertRaises(ValueError):
            self.run_pipeline(JsonFixtureSource(self.write_payload(payload)))
        self.store.close()
        self.assertEqual(self.query("SELECT COUNT(*) FROM parts"), [(0,)], "failed batch must not leak partial parts")
        self.assertEqual(self.query("SELECT COUNT(*) FROM quotes"), [(0,)], "failed batch must not leak partial quotes")

    def test_missing_price_and_lead_time_persist_as_null(self):
        payload = [{"supplier": "Northstar", "part_number": "ZZ-9", "brand": "Acme", "price": "N/A"}]
        self.run_pipeline(JsonFixtureSource(self.write_payload(payload)))
        rows = self.query("SELECT price, lead_time_days FROM quotes")
        self.assertEqual(rows, [(None, None)], "absent price/lead time must persist as NULL, not text")

    def test_rerun_is_idempotent(self):
        source_path = self.write_payload(
            [{"supplier": "Northstar", "part_number": "ZZ-1", "brand": "Acme", "price": "9.99"}]
        )
        first = self.run_pipeline(JsonFixtureSource(source_path))
        second = self.run_pipeline(JsonFixtureSource(source_path))
        self.assertEqual((first, second), (1, 1))
        self.assertEqual(self.query("SELECT COUNT(*) FROM parts"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM quotes"), [(1,)])

    def test_non_list_payload_is_rejected(self):
        path = self.write_payload({"items": []})
        with self.assertRaisesRegex(ValueError, "must be a list"):
            self.run_pipeline(JsonFixtureSource(path))

    def test_missing_required_field_raises_descriptive_error(self):
        payload = [{"supplier": "Northstar", "brand": "Acme"}]  # no part_number
        with self.assertRaises(ValueError, msg="malformed records should surface as ValueError, not KeyError"):
            self.run_pipeline(JsonFixtureSource(self.write_payload(payload)))

    def test_unrecognized_stock_status_persisted_as_unknown(self):
        payload = [{"supplier": "Northstar", "part_number": "ZZ-1", "brand": "Acme", "stock_status": "Ships Tuesday"}]
        self.run_pipeline(JsonFixtureSource(self.write_payload(payload)))
        self.assertEqual(self.query("SELECT stock_status FROM quotes"), [("unknown",)])


class TestCliContract(unittest.TestCase):
    """The documented CLI entry point must produce the database it reports."""

    def run_cli(self, *args: str):
        import os
        import subprocess
        import sys

        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
        return subprocess.run(
            [sys.executable, "-m", "parts_catalog.cli", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
        )

    def test_readme_command_persists_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "parts.sqlite3")
            result = self.run_cli("--fixture", "fixtures/supplier_payload.json", "--database", database)
            self.assertEqual(result.returncode, 0, result.stderr)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM parts").fetchone(), (1,))
                self.assertTrue(connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] >= 1)
            finally:
                connection.close()

    def test_stdout_is_machine_parseable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "parts.sqlite3")
            result = self.run_cli("--fixture", "fixtures/supplier_payload.json", "--database", database)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["processed"], 2)
            self.assertEqual(report["database"], database)


if __name__ == "__main__":
    unittest.main()
