"""End-to-end proof for the quarterly review: manual sign-off overlaid with
complete, missing-field and contradictory feed records, and a second import
pass that must not drift."""
from pathlib import Path

from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SEQUENCE = ["manual_signoff.json", "feed_complete.json",
            "feed_missing_fields.json", "feed_conflicting.json"]


def run(store, name, origin="feed"):
    return PartsPipeline(JsonFixtureSource(FIXTURES / name), store).run("USD", name, origin)


def snapshot(store):
    tables = {}
    for table in ("parts", "quotes", "field_provenance", "field_decisions"):
        rows = store.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4").fetchall()
        tables[table] = rows
    return tables


def test_manual_overlay_then_contradictory_feeds_and_reimport_no_drift(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    for name in SEQUENCE:
        run(store, name)
    first = snapshot(store)

    # Manual sign-off survives every feed record, including the ad-hoc rewrite.
    assert first["parts"] == [("AB1204", "ACME", "Sealed bearing 20 mm", "EA", 10)]
    assert all(row[3] == "manual" for row in first["field_provenance"])

    # The contradictory record's description and package_quantity were
    # rejected and recorded as reviewable conflicts; brand/unit matched.
    conflicts = [row for row in first["field_decisions"] if row[4] == "conflict"]
    assert {row[1] for row in conflicts} == {"description", "package_quantity"}
    assert all(row[7] == "manual" for row in conflicts)  # incumbent_origin kept

    # Missing fields were skipped, not defaulted over the sign-off.
    skipped = [row for row in first["field_decisions"] if row[4] == "skipped"]
    assert {row[1] for row in skipped} == {"description", "unit", "package_quantity"}

    # Quotes stayed independent supplier facts: all three feed prices are
    # stored, including the one from the record whose catalog fields lost.
    assert sorted(row[3] for row in first["quotes"]) == ["18.50", "18.75", "19.10"]

    # Second import of the whole sequence: zero drift in every table.
    for name in SEQUENCE:
        run(store, name)
    assert snapshot(store) == first


def test_manual_record_creates_no_quote(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    run(store, "manual_signoff.json")
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    review = store.review("AB1204")
    assert review["part"]["package_quantity"] == 10
    assert {row["field"] for row in review["provenance"]} == {
        "brand", "description", "unit", "package_quantity"}
    assert all(row["origin"] == "manual" for row in review["provenance"])


def test_missing_fields_do_not_erase_and_defaults_stay_unowned(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    run(store, "feed_missing_fields.json")
    # Contract defaults materialize for never-asserted fields, but provenance
    # shows only brand is actually owned by a source.
    assert store.connection.execute("SELECT * FROM parts").fetchall() == [
        ("AB1204", "ACME", "", "EA", 1)]
    provenance = store.review("AB1204")["provenance"]
    assert [row["field"] for row in provenance] == ["brand"]
    # The quote fact is stored even though most catalog fields were absent.
    assert store.connection.execute("SELECT price FROM quotes").fetchall() == [("18.75",)]


def test_feed_feed_conflict_latest_wins_logged_and_replay_stable(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    run(store, "supplier_payload.json")
    first = snapshot(store)
    # Two feed records disagree on description; the later record wins and the
    # disagreement is on the ledger as a reviewable override.
    assert first["parts"][0][2] == "Sealed bearing 20mm"
    overrides = [row for row in first["field_decisions"] if row[4] == "overridden"]
    assert {row[1] for row in overrides} == {"description"}
    assert store.review("AB1204")["provenance"]
    run(store, "supplier_payload.json")
    assert snapshot(store) == first


def test_feed_first_then_manual_signoff_locks_field(tmp_path):
    store = PartsStore(tmp_path / "catalog.sqlite3")
    run(store, "feed_conflicting.json")
    assert store.connection.execute("SELECT package_quantity FROM parts").fetchone()[0] == 20
    run(store, "manual_signoff.json")
    assert store.connection.execute("SELECT package_quantity FROM parts").fetchone()[0] == 10
    # Once signed off, a replay of the contradictory feed cannot move it.
    run(store, "feed_conflicting.json")
    assert store.connection.execute("SELECT package_quantity FROM parts").fetchone()[0] == 10
    review = store.review("AB1204")
    assert all(row["origin"] == "manual" for row in review["provenance"])
    # The contradictory quote remains stored as a supplier fact throughout.
    assert ("19.10",) in store.connection.execute("SELECT price FROM quotes").fetchall()
