from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from parts_catalog.models import NormalizedPart, Quote
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import ImportBatch, PartsStore, StorageStateError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "supplier_payload.json"

BASELINE_PAYLOAD = [
    {
        "supplier": "Contoso",
        "part_number": "BASE-001",
        "brand": "Northstar Ltd",
        "description": "Baseline gasket",
        "unit": "EA",
        "package_quantity": 1,
        "lead_time_days": 2,
        "stock_status": "In Stock",
        "price": "9.99",
    }
]

PART = NormalizedPart("PN1", "ACME", "Test part", "EA", 1)
QUOTE = Quote("PN1", "Supplier", "USD", Decimal("1.00"), 3, "available", "test")


class CrashBeforeQuoteBatch(ImportBatch):
    """Dies after the catalog write and before the quote write.

    Simulates the nightly replenishment job being killed mid-run, at the worst
    possible moment: the part row is already written, the quote row is not.
    """

    def save_quote(self, quote: Quote) -> None:
        raise RuntimeError("simulated process crash between catalog and quote writes")


class CrashBeforeQuoteStore(PartsStore):
    _batch_class = CrashBeforeQuoteBatch


def write_payload(path: Path, payload: list[dict]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def committed_rows(database: Path) -> tuple[set[str], list[tuple]]:
    """Read the database through a brand-new connection, as another process would."""
    connection = sqlite3.connect(database)
    try:
        parts = {row[0] for row in connection.execute("SELECT canonical_number FROM parts")}
        quotes = connection.execute("SELECT canonical_number, source_ref FROM quotes").fetchall()
    finally:
        connection.close()
    return parts, quotes


def test_interrupted_run_rolls_back_and_successful_rerun_publishes(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    baseline = write_payload(tmp_path / "baseline.json", BASELINE_PAYLOAD)

    # Previous batch: the data readers currently rely on.
    store = PartsStore(database)
    assert PartsPipeline(JsonFixtureSource(baseline), store).run(source_ref="baseline") == 1
    store.close()

    # Nightly run crashes between the catalog write and the quote write.
    crashing = CrashBeforeQuoteStore(database)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        PartsPipeline(JsonFixtureSource(FIXTURE), crashing).run(source_ref="nightly")
    crashing.close()

    # Seen through a fresh connection: the previous batch is intact and not a
    # single row of the interrupted batch leaked into the readable state.
    parts, quotes = committed_rows(database)
    assert parts == {"BASE001"}
    assert quotes == [("BASE001", "baseline")]

    # A successful rerun publishes the complete batch on top of the old data.
    store = PartsStore(database)
    assert PartsPipeline(JsonFixtureSource(FIXTURE), store).run(source_ref="nightly") == 2
    parts = {part.canonical_number: part for part in store.read_parts()}
    quotes = store.read_quotes()
    store.close()

    assert set(parts) == {"BASE001", "AB1204"}
    assert parts["AB1204"].brand == "ACME"
    # Both fixture rows canonicalize to AB1204; the later INSERT OR REPLACE wins.
    assert parts["AB1204"].description == "Sealed bearing 20mm"
    nightly = [quote for quote in quotes if quote.source_ref == "nightly"]
    assert len(nightly) == 1
    assert nightly[0].canonical_number == "AB1204"
    assert nightly[0].price == Decimal("18.75")
    assert any(quote.source_ref == "baseline" for quote in quotes)


def test_commit_only_on_clean_exit(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    with store.import_batch() as batch:
        batch.save_part(PART)
        batch.save_quote(QUOTE)
    assert batch.committed
    assert not batch.rolled_back
    assert (batch.parts_written, batch.quotes_written) == (1, 1)
    assert [part.canonical_number for part in store.read_parts()] == ["PN1"]
    assert [quote.canonical_number for quote in store.read_quotes()] == ["PN1"]
    store.close()


def test_batch_rolls_back_on_exception_and_rejects_further_writes(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    batch = store.import_batch()
    with pytest.raises(RuntimeError, match="boom"):
        with batch:
            batch.save_part(PART)
            raise RuntimeError("boom")
    assert batch.rolled_back
    assert not batch.committed
    # After close (rolled back), the batch refuses any more writes.
    with pytest.raises(StorageStateError):
        batch.save_part(PART)
    with pytest.raises(StorageStateError):
        batch.save_quote(QUOTE)
    assert store.read_parts() == []
    store.close()


def test_committed_batch_rejects_further_writes(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    with store.import_batch() as batch:
        batch.save_part(PART)
    assert batch.committed
    with pytest.raises(StorageStateError):
        batch.save_part(PART)
    store.close()


def test_writes_outside_the_context_manager_are_rejected(tmp_path):
    # Without an explicit BEGIN, a bare save must fail instead of
    # auto-committing a partial batch row by row.
    store = PartsStore(tmp_path / "catalog.sqlite3")
    batch = store.import_batch()
    with pytest.raises(StorageStateError):
        batch.save_part(PART)
    with pytest.raises(StorageStateError):
        batch.save_quote(QUOTE)
    store.close()


def test_only_one_batch_may_be_active_at_a_time(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    batch = store.import_batch()
    with pytest.raises(StorageStateError):
        store.import_batch()
    with batch:
        batch.save_part(PART)
    # Once the first batch is finished, the store accepts the next one.
    with store.import_batch() as second:
        second.save_part(NormalizedPart("PN2", "ACME", "Other part", "EA", 1))
    assert second.committed
    store.close()


def test_close_rolls_back_active_batch_and_is_idempotent(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    store = PartsStore(database)
    batch = store.import_batch()
    batch.__enter__()
    batch.save_part(PART)
    store.close()  # closing with an unfinished batch must roll it back
    assert batch.rolled_back
    store.close()  # idempotent
    with pytest.raises(StorageStateError):
        store.import_batch()
    with pytest.raises(StorageStateError):
        store.read_parts()
    with pytest.raises(StorageStateError):
        batch.save_part(PART)
    parts, _ = committed_rows(database)
    assert parts == set()


def test_store_offers_no_direct_write_or_commit_escape_hatch():
    # Callers must go through the unit of work; the store itself cannot be
    # used to trickle out half a batch.
    for leaked in ("commit", "rollback", "save_part", "save_quote"):
        assert not hasattr(PartsStore, leaked), leaked


def test_legacy_database_file_remains_readable_and_writable(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    # Build a database file exactly the way the original store did.
    legacy = sqlite3.connect(database)
    legacy.execute("""CREATE TABLE IF NOT EXISTS parts (
        canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
        unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
    )""")
    legacy.execute("""CREATE TABLE IF NOT EXISTS quotes (
        canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
        price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
        PRIMARY KEY (canonical_number, supplier, source_ref)
    )""")
    legacy.execute("INSERT INTO parts VALUES ('LEGACY1', 'ACME', 'Legacy seal', 'EA', 1)")
    legacy.execute(
        "INSERT INTO quotes VALUES ('LEGACY1', 'OldSupplier', 'USD', '12.34', 7, 'available', 'legacy-import')"
    )
    legacy.commit()
    legacy.close()

    store = PartsStore(database)
    parts = store.read_parts()
    assert [part.canonical_number for part in parts] == ["LEGACY1"]
    quotes = store.read_quotes()
    assert len(quotes) == 1
    assert quotes[0].price == Decimal("12.34")
    assert quotes[0].source_ref == "legacy-import"

    # The legacy file also accepts a new batch through the unit of work.
    with store.import_batch() as batch:
        batch.save_part(PART)
        batch.save_quote(QUOTE)
    assert batch.committed
    assert {part.canonical_number for part in store.read_parts()} == {"LEGACY1", "PN1"}
    store.close()


def test_cli_end_to_end_with_fixture(tmp_path, monkeypatch, capsys):
    from parts_catalog.cli import main

    database = tmp_path / "cli.sqlite3"
    monkeypatch.setattr(
        "sys.argv",
        ["parts-catalog", "--fixture", str(FIXTURE), "--database", str(database)],
    )
    main()
    out = capsys.readouterr().out
    assert "'processed': 2" in out

    store = PartsStore(database)
    assert {part.canonical_number for part in store.read_parts()} == {"AB1204"}
    assert [quote.source_ref for quote in store.read_quotes()] == [str(FIXTURE)]
    store.close()
