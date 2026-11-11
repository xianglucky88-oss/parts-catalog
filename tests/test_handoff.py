import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

import pytest

from parts_catalog import cli
from parts_catalog.handoff import EnvelopeError, load_envelope, parse_envelope
from parts_catalog.ingest import BatchIngestor
from parts_catalog.storage import PartsStore

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "batch_envelope.json"


def make_envelope(**overrides):
    payload = {
        "contract_version": "1.0",
        "batch_id": "batch-1",
        "supplier": "Northstar",
        "queried_at": "2026-11-11T09:30:00Z",
        "query": {"part_number": "AB-120 / 4"},
        "results": [],
    }
    payload.update(overrides)
    return payload


def ok_part(**overrides):
    part = {
        "supplier": "Northstar",
        "part_number": "AB-120 / 4",
        "brand": "Acme Inc.",
        "description": "Sealed bearing 20 mm",
        "unit": "ea",
        "package_quantity": 1,
        "lead_time_days": 4,
        "stock_status": "In Stock",
        "price": "18.50",
    }
    part.update(overrides)
    return {"status": "ok", "part": part}


def ingest(tmp_path, envelope):
    store = PartsStore(tmp_path / "parts.sqlite3")
    try:
        report = BatchIngestor(store).ingest(envelope)
        return report, store
    except Exception:
        store.close()
        raise


def test_parse_fixture_envelope():
    envelope = load_envelope(FIXTURE)
    assert envelope.batch_id == "northstar-2026-11-11-0001"
    assert envelope.supplier == "Northstar"
    assert envelope.query_part_number == "AB-120 / 4"
    assert [r.status for r in envelope.results] == ["ok", "error"]
    assert envelope.results[1].error.code == "extract_failed"


def test_parse_rejects_contract_violations():
    with pytest.raises(EnvelopeError):
        parse_envelope(make_envelope(batch_id=""))
    with pytest.raises(EnvelopeError):
        parse_envelope(make_envelope(contract_version="2.0"))
    with pytest.raises(EnvelopeError):
        parse_envelope(make_envelope(queried_at="not-a-date"))
    with pytest.raises(EnvelopeError):
        parse_envelope(make_envelope(results=[{"status": "unknown"}]))
    with pytest.raises(EnvelopeError):
        parse_envelope(make_envelope(results=[{"status": "ok"}]))
    with pytest.raises(EnvelopeError):
        parse_envelope(make_envelope(results=[{"status": "error", "error": {"code": "", "message": ""}}]))


def test_ingest_fixture_is_partial(tmp_path):
    report, store = ingest(tmp_path, load_envelope(FIXTURE))
    try:
        assert report.outcome == "partial"
        assert (report.received, report.imported, report.failed) == (2, 1, 1)
        assert report.errors[0].code == "extract_failed"
        row = store.connection.execute("SELECT canonical_number, brand FROM parts").fetchone()
        assert row == ("AB1204", "ACME")
        quote = store.connection.execute("SELECT price, source_ref FROM quotes").fetchone()
        assert quote == ("18.50", "northstar-2026-11-11-0001")
        assert store.batch_status("northstar-2026-11-11-0001")["status"] == "partial"
    finally:
        store.close()


def test_duplicate_batch_is_idempotent(tmp_path):
    envelope = parse_envelope(make_envelope(results=[ok_part()]))
    first, store = ingest(tmp_path, envelope)
    assert first.outcome == "imported"
    second = BatchIngestor(store).ingest(envelope)
    try:
        assert second.outcome == "duplicate"
        assert second.imported == 0
        assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0] == 1
    finally:
        store.close()


def test_empty_batch_is_recorded(tmp_path):
    report, store = ingest(tmp_path, parse_envelope(make_envelope()))
    try:
        assert report.outcome == "empty"
        assert (report.received, report.imported, report.failed) == (0, 0, 0)
        assert store.batch_status("batch-1")["status"] == "empty"
        assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    finally:
        store.close()


def test_all_error_batch_fails_without_writes(tmp_path):
    results = [{"status": "error", "error": {"code": "extract_failed", "message": "boom"}}]
    report, store = ingest(tmp_path, parse_envelope(make_envelope(results=results)))
    try:
        assert report.outcome == "failed"
        assert report.imported == 0
        assert store.connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 0
        assert store.batch_status("batch-1")["status"] == "failed"
    finally:
        store.close()


def test_malformed_row_becomes_failed_result_not_crash(tmp_path):
    results = [ok_part(), ok_part(part_number="CD-9", price="not-a-price")]
    report, store = ingest(tmp_path, parse_envelope(make_envelope(results=results)))
    try:
        assert report.outcome == "partial"
        assert (report.imported, report.failed) == (1, 1)
        assert report.errors[0].index == 1
        assert report.errors[0].code == "normalize_failed"
    finally:
        store.close()


def test_cli_batch_then_duplicate(tmp_path):
    db = str(tmp_path / "cli.sqlite3")
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["--batch", str(FIXTURE), "--database", db])
    report = json.loads(out.getvalue())
    assert report["outcome"] == "partial"
    assert report["database"] == db
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["--batch", str(FIXTURE), "--database", db])
    assert json.loads(out.getvalue())["outcome"] == "duplicate"


def test_cli_rejects_invalid_envelope(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make_envelope(batch_id="")), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--batch", str(bad), "--database", str(tmp_path / "x.sqlite3")])
    assert excinfo.value.code == 1


def test_cli_manual_fixture_import_unchanged(tmp_path):
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "supplier_payload.json"
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["--fixture", str(fixture), "--database", str(tmp_path / "m.sqlite3")])
    assert "'processed': 2" in out.getvalue()
