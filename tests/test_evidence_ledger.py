from __future__ import annotations
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from parts_catalog.audit import entity_trail, verify, verify_input
from parts_catalog.evidence import input_digest
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore, quote_entity_key

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PAYLOAD = FIXTURES / "supplier_payload.json"
SHUFFLED = FIXTURES / "supplier_payload_shuffled.json"
SINGLE = FIXTURES / "supplier_payload_one.json"


class _TempDB:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "catalog.sqlite3"

    def cleanup(self) -> None:
        self.tmp.cleanup()


class NormalImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDB()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_import_writes_facts_and_verifiable_evidence(self) -> None:
        store = PartsStore(self.db.path)
        result = PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        self.assertFalse(result.replayed)
        # 2 raw records -> one part entry + one quote entry each.
        self.assertEqual(result.entries_written, 4)
        report = verify(store)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.entries_checked, 4)
        self.assertEqual(report.runs_checked, 1)

        # The final quote is readable from the historical business table...
        quote = store.get_quote("AB1204", "Northstar", str(PAYLOAD))
        # INSERT OR REPLACE means the second raw record (18.75) is the final
        # state; the ledger below is what proves the earlier 18.50 existed.
        self.assertEqual(quote["price"], "18.75")
        # ...and the ledger proves which input record and rules produced it.
        trail = entity_trail(
            store, "quote", quote_entity_key("AB1204", "Northstar", str(PAYLOAD))
        )
        self.assertEqual(len(trail), 2)  # both raw records hit the same quote key
        last = trail[-1]
        # The quote entry records the quote-level rules and field steps...
        self.assertIn("text.upper-v1", last["rule_ids"])
        self.assertIn("quote.source-ref-v1", last["rule_ids"])
        currency_steps = [t for t in last["transforms"] if t["field_name"] == "currency"]
        self.assertEqual(currency_steps[0]["before"], "USD")
        self.assertEqual(currency_steps[0]["after"], "USD")
        price_steps = [t for t in last["transforms"] if t["field_name"] == "price"]
        self.assertEqual(price_steps[0]["before"], "18.75")
        self.assertEqual(price_steps[0]["after"], "18.75")
        # ...while the part entry on the same raw record carries the
        # normalization rules (canonical part number, decimal price, alias).
        part_trail = entity_trail(store, "part", "AB1204")
        part_rules = {rule for event in part_trail for rule in event["rule_ids"]}
        self.assertIn("part_number.canonical-alnum-v1", part_rules)
        self.assertIn("price.decimal-v1", part_rules)
        self.assertIn("brand.canonical-alias-v1", part_rules)
        # The first event retains the overwritten value as history.
        self.assertEqual(trail[0]["after"]["price"], "18.50")
        self.assertEqual(trail[1]["before"]["price"], "18.50")
        store.close()

    def test_audit_rebinds_external_input_file(self) -> None:
        store = PartsStore(self.db.path)
        PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        report = verify_input(store, JsonFixtureSource(PAYLOAD).read())
        self.assertTrue(report.ok, report.to_dict())

        # A modified input no longer matches the hashed raw records.
        tampered = JsonFixtureSource(PAYLOAD).read()
        tampered[0]["price"] = "999.99"
        bad = verify_input(store, tampered)
        self.assertFalse(bad.ok)
        self.assertTrue(any(f.code == "input.hash_mismatch" for f in bad.findings))
        store.close()

    def test_rewrite_history_is_kept_as_before_after_evidence(self) -> None:
        store = PartsStore(self.db.path)
        PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))

        # A later import changes the price while targeting the same business
        # key (same part/supplier/source_ref). It is a different input digest
        # and therefore a new run that overwrites the row; the ledger keeps
        # both versions with before/after snapshots.
        modified = JsonFixtureSource(PAYLOAD).read()
        modified[0]["price"] = "19.99"
        modified_path = Path(self.db.tmp.name) / "modified_payload.json"
        modified_path.write_text(json.dumps(modified), encoding="utf-8")
        PartsPipeline(JsonFixtureSource(modified_path), store).run("USD", str(PAYLOAD))

        key = quote_entity_key("AB1204", "Northstar", str(PAYLOAD))
        trail = entity_trail(store, "quote", key)
        self.assertEqual(len(trail), 4)
        self.assertEqual(trail[0]["before"], None)
        self.assertEqual(trail[1]["before"]["price"], "18.50")
        self.assertEqual(trail[1]["after"]["price"], "18.75")
        self.assertEqual(trail[2]["before"]["price"], "18.75")
        self.assertEqual(trail[2]["after"]["price"], "19.99")
        self.assertTrue(verify(store).ok)
        store.close()


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDB()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_identical_replay_adds_no_facts_and_no_evidence(self) -> None:
        store = PartsStore(self.db.path)
        first = PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        ledger_after_first = store.load_ledger()
        runs_after_first = store.load_runs()
        parts_after_first = store.connection.execute("SELECT * FROM parts").fetchall()
        quotes_after_first = store.connection.execute("SELECT * FROM quotes").fetchall()

        second = PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        self.assertTrue(second.replayed)
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.entries_written, 0)

        self.assertEqual(store.load_ledger(), ledger_after_first)
        self.assertEqual(store.load_runs(), runs_after_first)
        self.assertEqual(store.connection.execute("SELECT * FROM parts").fetchall(), parts_after_first)
        self.assertEqual(store.connection.execute("SELECT * FROM quotes").fetchall(), quotes_after_first)
        self.assertTrue(verify(store).ok)
        store.close()


class OutOfOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDB()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_shuffled_input_distinct_digest_but_consistent_chain(self) -> None:
        store = PartsStore(self.db.path)
        ordered = PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        shuffled = PartsPipeline(JsonFixtureSource(SHUFFLED), store).run("USD", str(SHUFFLED))

        # Different input ordering => different input digest => distinct run.
        self.assertNotEqual(ordered.input_hash, shuffled.input_hash)
        self.assertNotEqual(ordered.run_id, shuffled.run_id)
        self.assertTrue(verify(store).ok)

        # The raw_index inside each entry preserves where the record sat in
        # its own import, so out-of-order arrival remains reconstructable.
        rows = store.load_ledger()
        run_rows = [r for r in rows if r["run_id"] == shuffled.run_id]
        # Entries alternate part/quote per record: each raw record contributes
        # two entries carrying its own position.
        part_rows = [r for r in run_rows if r["entity_type"] == "part"]
        self.assertEqual([r["raw_index"] for r in part_rows], [0, 1])
        self.assertEqual(part_rows[0]["entity_key"], "AB1204")
        store.close()

    def test_reordering_stored_ledger_rows_is_detected(self) -> None:
        store = PartsStore(self.db.path)
        PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        # Tamper: physically swap two ledger rows (simulating reordering).
        store.connection.execute(
            "UPDATE evidence_ledger SET seq = -seq WHERE seq = 1"
        )
        store.connection.execute(
            "UPDATE evidence_ledger SET seq = 1 WHERE seq = 2"
        )
        store.connection.execute(
            "UPDATE evidence_ledger SET seq = 2 WHERE seq = -1"
        )
        report = verify(store)
        self.assertFalse(report.ok)
        codes = {f.code for f in report.findings}
        self.assertIn("chain.link_broken", codes)
        # The blast radius starts at the first broken entry and never narrows.
        broken = next(f for f in report.findings if f.code == "chain.link_broken")
        self.assertIn(1, broken.blast_radius)
        self.assertEqual(max(broken.blast_radius), report.chain_head_seq)
        store.close()


class TamperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDB()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_mutated_evidence_is_detected_with_blast_radius(self) -> None:
        store = PartsStore(self.db.path)
        PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        # Tamper directly with a stored after_snapshot, bypassing the pipeline.
        row = store.connection.execute(
            "SELECT after_snapshot FROM evidence_ledger WHERE seq = 2"
        ).fetchone()
        mutated_snapshot = json.loads(row["after_snapshot"])
        mutated_snapshot["price"] = "0.01"
        store.connection.execute(
            "UPDATE evidence_ledger SET after_snapshot = ? WHERE seq = 2",
            (json.dumps(mutated_snapshot, sort_keys=True, separators=(",", ":")),),
        )
        report = verify(store)
        self.assertFalse(report.ok)
        finding = next(f for f in report.findings if f.code == "hash.tampered")
        self.assertEqual(finding.seq, 2)
        # Everything from seq 2 onward is within the affected range.
        self.assertEqual(finding.blast_radius, [2, 3, 4])
        store.close()

    def test_business_table_drift_is_detected(self) -> None:
        store = PartsStore(self.db.path)
        PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        store.connection.execute(
            "UPDATE quotes SET price = '0.01' WHERE canonical_number = 'AB1204'"
        )
        report = verify(store)
        self.assertFalse(report.ok)
        self.assertTrue(any(f.code == "state.drift" for f in report.findings))
        store.close()

    def test_deleted_entry_is_detected_as_gap(self) -> None:
        store = PartsStore(self.db.path)
        PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        store.connection.execute("DELETE FROM evidence_ledger WHERE seq = 2")
        report = verify(store)
        self.assertFalse(report.ok)
        codes = {f.code for f in report.findings}
        self.assertIn("sequence.gap", codes)
        self.assertIn("chain.link_broken", codes)
        store.close()


class InterruptedWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDB()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_fault_before_commit_leaves_no_trace(self) -> None:
        store = PartsStore(self.db.path)

        def boom(_store: PartsStore) -> None:
            raise RuntimeError("simulated crash before commit")

        with self.assertRaises(RuntimeError):
            PartsPipeline(JsonFixtureSource(PAYLOAD), store, fault_hook=boom).run("USD", str(PAYLOAD))

        # No business facts, no evidence, and no run marker: an unfinished
        # write cannot masquerade as a completed import.
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 0)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0], 0)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0], 0)

        # The database is still usable and the same import can run cleanly.
        result = PartsPipeline(JsonFixtureSource(PAYLOAD), store).run("USD", str(PAYLOAD))
        self.assertFalse(result.replayed)
        self.assertTrue(verify(store).ok)
        store.close()


class LegacyDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDB()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_historical_sqlite_database_remains_readable(self) -> None:
        # Build a database exactly as the pre-ledger version would have: only
        # the two original tables, populated with INSERT OR REPLACE semantics.
        legacy = sqlite3.connect(self.db.path)
        legacy.execute("""CREATE TABLE parts (
            canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
            unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
        )""")
        legacy.execute("""CREATE TABLE quotes (
            canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
            price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
            PRIMARY KEY (canonical_number, supplier, source_ref)
        )""")
        legacy.execute(
            "INSERT INTO parts VALUES (?, ?, ?, ?, ?)",
            ("LEG1", "LEGACY", "old part", "EA", 1),
        )
        legacy.execute(
            "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("LEG1", "OldCo", "USD", "9.99", 3, "available", "legacy-ref"),
        )
        legacy.commit()
        legacy.close()

        # Opening it with the new store must add ledger tables without
        # touching the existing rows.
        store = PartsStore(self.db.path)
        self.assertEqual(store.get_part("LEG1")["brand"], "LEGACY")
        self.assertEqual(store.get_quote("LEG1", "OldCo", "legacy-ref")["price"], "9.99")
        self.assertEqual(store.chain_head(), (0, "GENESIS"))

        # New imports work alongside the historical, un-evidenced rows.
        result = PartsPipeline(JsonFixtureSource(SINGLE), store).run("USD", str(SINGLE))
        self.assertTrue(verify(store).ok)
        self.assertEqual(result.entries_written, 2)
        # The legacy row is still exactly as it was.
        self.assertEqual(store.get_quote("LEG1", "OldCo", "legacy-ref")["price"], "9.99")
        store.close()


if __name__ == "__main__":
    unittest.main()
