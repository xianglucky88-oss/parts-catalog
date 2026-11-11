import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from parts_catalog.decision import (
    INELIGIBLE_REASONS,
    SCHEMA_VERSION,
    TIE_BREAK_DIMENSIONS,
    build_decision_projection,
    project_to_file,
)
from parts_catalog.models import NormalizedPart, Quote
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore

SRC = Path(__file__).resolve().parents[1] / "src"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "decision_cases.json"


class ProjectionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog.sqlite3"
        self.store = PartsStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()

    def part(self, canonical="X1", brand="ACME") -> NormalizedPart:
        return NormalizedPart(canonical, brand, "desc", "EA", 1)

    def quote(
        self,
        supplier,
        price,
        canonical="X1",
        currency="USD",
        lead_time_days=None,
        stock_status="available",
        source_ref="fixture",
    ) -> Quote:
        return Quote(
            canonical_number=canonical,
            supplier=supplier,
            currency=currency,
            price=Decimal(price) if price is not None else None,
            lead_time_days=lead_time_days,
            stock_status=stock_status,
            source_ref=source_ref,
        )

    def save(self, *quotes: Quote, canonical="X1", brand="ACME") -> None:
        self.store.save_part(self.part(canonical, brand))
        for quote in quotes:
            self.store.save_quote(quote)
        self.store.commit()

    def decision(self, canonical="X1", currency="USD"):
        projection = build_decision_projection(self.db_path, currency)
        by_number = {item["canonical_number"]: item for item in projection["decisions"]}
        return projection, by_number[canonical]


class MultiSupplierTests(ProjectionTestBase):
    def test_lowest_price_wins_and_losers_carry_decimal_deltas(self):
        self.save(
            self.quote("Alpha", "10.00", lead_time_days=2),
            self.quote("Bravo", "8.00", lead_time_days=7),
            self.quote("Charlie", "12.00", lead_time_days=1),
        )
        _, decision = self.decision()

        self.assertEqual(decision["status"], "recommended")
        self.assertEqual(decision["eligible_count"], 3)
        self.assertEqual(decision["ineligible_count"], 0)
        self.assertEqual(decision["recommendation"]["supplier"], "Bravo")
        self.assertEqual(decision["recommendation"]["price"], "8.00")

        ranks = {entry["supplier"]: entry["rank"] for entry in decision["considered"]}
        self.assertEqual(ranks, {"Bravo": 1, "Alpha": 2, "Charlie": 3})

        by_supplier = {entry["supplier"]: entry for entry in decision["considered"]}
        self.assertEqual(by_supplier["Bravo"]["outcome"], "selected")
        self.assertIsNone(by_supplier["Bravo"]["reason_code"])
        self.assertEqual(by_supplier["Alpha"]["reason_code"], "higher_price")
        self.assertEqual(by_supplier["Alpha"]["price_delta"], "2.00")
        self.assertEqual(by_supplier["Charlie"]["reason_code"], "higher_price")
        self.assertEqual(by_supplier["Charlie"]["price_delta"], "4.00")
        self.assertNotIn("tie_broken_by", by_supplier["Alpha"])

    def test_price_delta_uses_exact_decimal_arithmetic(self):
        self.save(
            self.quote("Alpha", "18.50"),
            self.quote("Bravo", "17.25"),
        )
        _, decision = self.decision()
        loser = next(entry for entry in decision["considered"] if entry["supplier"] == "Alpha")
        self.assertEqual(loser["price_delta"], "1.25")
        # Prices must be text, never binary floats.
        self.assertIsInstance(decision["recommendation"]["price"], str)


class TieBreakTests(ProjectionTestBase):
    def test_equal_price_broken_by_lead_time(self):
        self.save(
            self.quote("Alpha", "9.99", lead_time_days=5),
            self.quote("Bravo", "9.99", lead_time_days=3),
        )
        _, decision = self.decision()
        self.assertEqual(decision["recommendation"]["supplier"], "Bravo")
        loser = decision["considered"][1]
        self.assertEqual(loser["reason_code"], "tie_broken")
        self.assertEqual(loser["tie_broken_by"], ["lead_time_days"])

    def test_equal_price_and_lead_broken_by_supplier_name(self):
        self.save(
            self.quote("Zeta", "9.99", lead_time_days=4),
            self.quote("Alpha", "9.99", lead_time_days=4),
        )
        _, decision = self.decision()
        self.assertEqual(decision["recommendation"]["supplier"], "Alpha")
        loser = decision["considered"][1]
        self.assertEqual(loser["tie_broken_by"], ["supplier"])

    def test_unknown_lead_time_sorts_last(self):
        self.save(
            self.quote("Alpha", "9.99", lead_time_days=None),
            self.quote("Bravo", "9.99", lead_time_days=9),
        )
        _, decision = self.decision()
        self.assertEqual(decision["recommendation"]["supplier"], "Bravo")
        self.assertEqual(decision["considered"][1]["tie_broken_by"], ["lead_time_days"])

    def test_identical_up_to_supplier_broken_by_source_ref(self):
        self.save(
            self.quote("SameCo", "9.99", lead_time_days=4, source_ref="zzz"),
            self.quote("SameCo", "9.99", lead_time_days=4, source_ref="aaa"),
        )
        _, decision = self.decision()
        self.assertEqual(decision["recommendation"]["source_ref"], "aaa")
        loser = decision["considered"][1]
        self.assertEqual(loser["tie_broken_by"], ["source_ref"])

    def test_tie_break_dimensions_are_documented_in_order(self):
        self.assertEqual(TIE_BREAK_DIMENSIONS, ("lead_time_days", "supplier", "source_ref"))


class IneligibleTests(ProjectionTestBase):
    def test_missing_price_is_ineligible_not_a_loser(self):
        self.save(
            self.quote("Priced", "5.00"),
            self.quote("NoPrice", None),
        )
        _, decision = self.decision()
        self.assertEqual(decision["eligible_count"], 1)
        self.assertEqual(decision["ineligible_count"], 1)
        self.assertEqual(decision["recommendation"]["supplier"], "Priced")
        self.assertEqual(decision["ineligible"][0]["supplier"], "NoPrice")
        self.assertEqual(decision["ineligible"][0]["reason_code"], "price_missing")
        considered_suppliers = {entry["supplier"] for entry in decision["considered"]}
        self.assertNotIn("NoPrice", considered_suppliers)

    def test_unavailable_stock_is_ineligible_even_when_cheapest(self):
        self.save(
            self.quote("Ghost", "1.00", stock_status="unavailable"),
            self.quote("Real", "9.00", stock_status="available"),
        )
        _, decision = self.decision()
        self.assertEqual(decision["recommendation"]["supplier"], "Real")
        self.assertEqual(decision["ineligible"][0]["reason_code"], "unavailable_stock")

    def test_backorder_and_unknown_stock_remain_eligible(self):
        self.save(
            self.quote("Back", "4.00", stock_status="backorder"),
            self.quote("Mystery", "4.00", stock_status="unknown"),
        )
        _, decision = self.decision()
        self.assertEqual(decision["eligible_count"], 2)
        self.assertEqual(decision["ineligible_count"], 0)

    def test_currency_mismatch_is_ineligible_and_currency_arg_flips_decision(self):
        self.save(
            self.quote("Domestic", "10.00", currency="USD"),
            self.quote("Foreign", "2.00", currency="EUR"),
        )
        _, usd = self.decision(currency="USD")
        self.assertEqual(usd["recommendation"]["supplier"], "Domestic")
        self.assertEqual(usd["ineligible"][0]["reason_code"], "currency_mismatch")
        self.assertEqual(usd["ineligible"][0]["supplier"], "Foreign")

        _, eur = self.decision(currency="EUR")
        self.assertEqual(eur["recommendation"]["supplier"], "Foreign")
        self.assertEqual(eur["ineligible"][0]["reason_code"], "currency_mismatch")
        self.assertEqual(eur["ineligible"][0]["supplier"], "Domestic")

    def test_unparseable_stored_price_is_invalid_price(self):
        self.save(self.quote("Real", "9.00"))
        # Seed a corrupt row directly; the projection must not crash on it.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("X1", "Broken", "USD", "abc", None, "available", "fixture"),
            )
            conn.commit()
        _, decision = self.decision()
        reasons = {entry["supplier"]: entry["reason_code"] for entry in decision["ineligible"]}
        self.assertEqual(reasons["Broken"], "invalid_price")
        self.assertEqual(decision["recommendation"]["supplier"], "Real")

    def test_reason_codes_are_stable_and_documented(self):
        self.assertEqual(
            set(INELIGIBLE_REASONS),
            {"price_missing", "invalid_price", "unavailable_stock", "currency_mismatch"},
        )


class NoEligibleTests(ProjectionTestBase):
    def test_only_ineligible_records_yields_no_eligible_candidate(self):
        self.save(
            self.quote("NoPrice", None, stock_status="available"),
            self.quote("Ghost", "1.00", stock_status="unavailable"),
        )
        _, decision = self.decision()
        self.assertEqual(decision["status"], "no_eligible_candidate")
        self.assertIsNone(decision["recommendation"])
        self.assertEqual(decision["considered"], [])
        self.assertEqual(decision["eligible_count"], 0)
        self.assertEqual(decision["ineligible_count"], 2)

    def test_part_without_any_quotes_is_still_projected(self):
        self.store.save_part(self.part("LONELY", "ACME"))
        self.store.commit()
        projection = build_decision_projection(self.db_path)
        decision = next(item for item in projection["decisions"] if item["canonical_number"] == "LONELY")
        self.assertEqual(decision["status"], "no_eligible_candidate")
        self.assertEqual(decision["candidate_count"], 0)
        self.assertEqual(decision["ineligible"], [])


class DeterminismTests(ProjectionTestBase):
    def _row_specs(self):
        return [
            ("Alpha", "9.99", 3, "available", "USD", "src-2"),
            ("Bravo", "9.99", 3, "available", "USD", "src-1"),
            ("Charlie", "7.50", None, "backorder", "USD", "src-1"),
            ("Delta", "7.50", 1, "available", "USD", "src-9"),
            ("Echo", None, 2, "unknown", "USD", "src-1"),
            ("Foxtrot", "1.00", 1, "unavailable", "USD", "src-1"),
            ("Golf", "6.00", 1, "available", "EUR", "src-1"),
        ]

    def _build_database(self, path: Path, ordering):
        store = PartsStore(path)
        store.save_part(NormalizedPart("X1", "ACME", "desc", "EA", 1))
        for supplier, price, lead, status, currency, source_ref in ordering:
            store.save_quote(
                Quote(
                    canonical_number="X1",
                    supplier=supplier,
                    currency=currency,
                    price=Decimal(price) if price is not None else None,
                    lead_time_days=lead,
                    stock_status=status,
                    source_ref=source_ref,
                )
            )
        store.commit()
        store.close()

    def test_projection_is_identical_for_every_insertion_order(self):
        specs = self._row_specs()
        reference_path = Path(self.tmp.name) / "ref.sqlite3"
        self._build_database(reference_path, specs)
        reference = json.dumps(build_decision_projection(reference_path), ensure_ascii=False, sort_keys=True)

        rng = random.Random(20261111)
        for attempt in range(8):
            shuffled = specs[:]
            rng.shuffle(shuffled)
            path = Path(self.tmp.name) / f"perm-{attempt}.sqlite3"
            self._build_database(path, shuffled)
            candidate = json.dumps(build_decision_projection(path), ensure_ascii=False, sort_keys=True)
            self.assertEqual(candidate, reference)

    def test_decisions_and_ineligible_lists_are_sorted(self):
        self.store.save_part(NormalizedPart("Z9", "ACME", "d", "EA", 1))
        self.store.save_part(NormalizedPart("A1", "ACME", "d", "EA", 1))
        self.store.save_quote(self.quote("Zeta", None, canonical="Z9"))
        self.store.save_quote(self.quote("Alpha", None, canonical="A1"))
        self.store.commit()
        projection = build_decision_projection(self.db_path)
        numbers = [item["canonical_number"] for item in projection["decisions"]]
        self.assertEqual(numbers, sorted(numbers))

    def test_projection_does_not_modify_the_database(self):
        self.save(self.quote("Alpha", "10.00"), self.quote("Bravo", "8.00"))
        self.store.close()
        before = Path(self.db_path).read_bytes()
        build_decision_projection(self.db_path)
        project_to_file(self.db_path, Path(self.tmp.name) / "out.json")
        after = Path(self.db_path).read_bytes()
        self.assertEqual(before, after)


class SchemaAndCliTests(ProjectionTestBase):
    def test_envelope_schema(self):
        self.save(self.quote("Alpha", "10.00"))
        projection = build_decision_projection(self.db_path)
        self.assertEqual(projection["schema_version"], SCHEMA_VERSION)
        self.assertEqual(projection["decision_currency"], "USD")
        self.assertIsInstance(projection["decisions"], list)
        decision = projection["decisions"][0]
        self.assertEqual(
            set(decision),
            {
                "canonical_number",
                "status",
                "candidate_count",
                "eligible_count",
                "ineligible_count",
                "recommendation",
                "ineligible",
                "considered",
            },
        )

    def test_project_to_file_writes_machine_readable_json(self):
        self.save(self.quote("Alpha", "10.00"), self.quote("Bravo", "8.00"))
        output = Path(self.tmp.name) / "decision.json"
        project_to_file(self.db_path, output)
        loaded = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(loaded["decisions"][0]["recommendation"]["supplier"], "Bravo")

    def _run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "parts_catalog.cli", *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    def test_cli_project_stdout(self):
        self.save(self.quote("Alpha", "10.00"), self.quote("Bravo", "8.00"))
        self.store.close()
        result = self._run_cli("--project", "--database", str(self.db_path))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision_currency"], "USD")
        self.assertEqual(payload["decisions"][0]["recommendation"]["price"], "8.00")

    def test_cli_project_output_file(self):
        self.save(self.quote("Alpha", "10.00"))
        self.store.close()
        output = Path(self.tmp.name) / "cli-out.json"
        self._run_cli("--project", "--database", str(self.db_path), "--output", str(output))
        self.assertTrue(output.exists())
        self.assertEqual(json.loads(output.read_text())["schema_version"], SCHEMA_VERSION)


class FixtureIntegrationTests(unittest.TestCase):
    def test_import_then_project_on_local_decision_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.sqlite3"
            store = PartsStore(db_path)
            try:
                count = PartsPipeline(JsonFixtureSource(FIXTURE), store).run("USD", str(FIXTURE))
            finally:
                store.close()
            self.assertEqual(count, 9)

            projection = build_decision_projection(db_path)
            decisions = {item["canonical_number"]: item for item in projection["decisions"]}

            # AB1204: BoltCo and Acme Direct tie at 17.25; Acme Direct has
            # the shorter lead time and wins. MegaParts has no price and
            # GhostSupply is unavailable, so neither appears as a loser.
            ab = decisions["AB1204"]
            self.assertEqual(ab["recommendation"]["supplier"], "Acme Direct")
            self.assertEqual(ab["recommendation"]["price"], "17.25")
            self.assertEqual(ab["eligible_count"], 3)
            self.assertEqual(ab["ineligible_count"], 2)
            self.assertEqual(
                {entry["reason_code"] for entry in ab["ineligible"]},
                {"price_missing", "unavailable_stock"},
            )
            second = next(entry for entry in ab["considered"] if entry["supplier"] == "BoltCo")
            self.assertEqual(second["reason_code"], "tie_broken")
            self.assertEqual(second["tie_broken_by"], ["lead_time_days"])
            northstar = next(entry for entry in ab["considered"] if entry["supplier"] == "Northstar")
            self.assertEqual(northstar["reason_code"], "higher_price")
            self.assertEqual(northstar["price_delta"], "1.25")

            # CD5500: full three-way price+lead tie; supplier name decides.
            cd = decisions["CD5500"]
            self.assertEqual(cd["recommendation"]["supplier"], "Acme Direct")
            self.assertEqual({entry["reason_code"] for entry in cd["considered"][1:]}, {"tie_broken"})

            # EF9: only an unavailable, unpriced record -> no candidate.
            ef = decisions["EF9"]
            self.assertEqual(ef["status"], "no_eligible_candidate")
            self.assertIsNone(ef["recommendation"])
            self.assertEqual(ef["ineligible_count"], 1)


if __name__ == "__main__":
    unittest.main()
