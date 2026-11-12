from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def run_cli(*args, expect=0):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "parts_catalog.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    assert proc.returncode == expect, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    return proc


def fixture(name):
    return str(FIXTURES / name)


def test_cli_replays_overnight_incident(tmp_path):
    """The morning-after replay: baseline round published, overnight round
    half finished, procurement keeps trusting the baseline until the retry
    completes the pinned scope."""
    db = str(tmp_path / "catalog.sqlite3")
    scope = fixture("scope_manifest.json")

    out = json.loads(run_cli(
        "--fixture", fixture("baseline_delivery.json"), "--scope", scope,
        "--run-id", "night-0", "--valid-until", "2099-01-01T00:00:00+00:00",
        "--database", db,
    ).stdout)
    assert out["status"] == "published"

    # the overnight run only finishes half of the pinned queries
    out = json.loads(run_cli(
        "--fixture", fixture("partial_delivery.json"), "--scope", scope,
        "--run-id", "night-1", "--database", db,
    ).stdout)
    assert out["status"] == "collecting"
    assert {(p["supplier"], p["canonical_number"]) for p in out["pending"]} == {
        ("Acme Direct", "AB1204"),
        ("Acme Direct", "CS770"),
    }

    # morning report: still the last complete round, nobody marked "no quote"
    view = json.loads(run_cli("--view", "--database", db).stdout)
    assert len(view) == 4
    assert all(row["run_id"] == "night-0" for row in view)
    assert all(row["price"] is not None for row in view)
    assert all(row["visibility"] == "current" for row in view)

    # the remaining queries arrive; the SAME run id is resumed and completes
    out = json.loads(run_cli(
        "--fixture", fixture("remaining_delivery.json"), "--scope", scope,
        "--run-id", "night-1", "--database", db,
    ).stdout)
    assert out["status"] == "published"

    view = { (row["supplier"], row["canonical_number"]): row
             for row in json.loads(run_cli("--view", "--database", db).stdout) }
    assert view[("Northstar", "AB1204")]["price"] == "18.90"
    assert view[("Acme Direct", "AB1204")]["price"] == "19.30"
    # explicit supplier no-quote: published with price null, not a missing row
    explicit = view[("Acme Direct", "CS770")]
    assert explicit["price"] is None
    assert explicit["stock_status"] == "unavailable"
    assert explicit["run_id"] == "night-1"

    # retransmitting the finished round changes nothing
    out = json.loads(run_cli(
        "--fixture", fixture("remaining_delivery.json"), "--scope", scope,
        "--run-id", "night-1", "--database", db,
    ).stdout)
    assert out["status"] == "already_published"


def test_cli_duplicate_delivery_is_idempotent(tmp_path):
    db = str(tmp_path / "catalog.sqlite3")
    scope = fixture("scope_manifest.json")
    for _ in range(2):
        out = json.loads(run_cli(
            "--fixture", fixture("duplicate_delivery.json"), "--scope", scope,
            "--run-id", "run-dup", "--database", db,
        ).stdout)
        assert out["status"] == "collecting"
    state = json.loads(run_cli("--pending", "run-dup", "--database", db).stdout)
    assert state["staged"] == 1
    assert len(state["pending"]) == 3


def test_cli_faulty_payload_is_rejected_and_retryable(tmp_path):
    db = str(tmp_path / "catalog.sqlite3")
    scope = fixture("scope_manifest.json")

    proc = run_cli(
        "--fixture", fixture("faulty_payload.json"), "--scope", scope,
        "--run-id", "broken-1", "--database", db, expect=2,
    )
    err = json.loads(proc.stderr)
    assert err["type"] == "DeliveryError"
    assert "record 1" in err["error"] and "record 2" in err["error"]

    # nothing from the faulty batch was staged; the run waits for a fixed retry
    state = json.loads(run_cli("--pending", "broken-1", "--database", db).stdout)
    assert state["status"] == "collecting"
    assert state["staged"] == 0
    assert len(state["pending"]) == 4

    out = json.loads(run_cli(
        "--fixture", fixture("baseline_delivery.json"), "--scope", scope,
        "--run-id", "broken-1", "--database", db,
    ).stdout)
    assert out["status"] == "published"


def test_cli_view_marks_expired_rows(tmp_path):
    db = str(tmp_path / "catalog.sqlite3")
    out = json.loads(run_cli(
        "--fixture", fixture("baseline_delivery.json"), "--scope", fixture("scope_manifest.json"),
        "--run-id", "old-run", "--valid-until", "2000-01-01T00:00:00+00:00",
        "--database", db,
    ).stdout)
    assert out["status"] == "published"
    view = json.loads(run_cli("--view", "--database", db).stdout)
    assert len(view) == 4
    assert all(row["visibility"] == "expired" for row in view)


def test_cli_expire_source(tmp_path):
    db = str(tmp_path / "catalog.sqlite3")
    run_cli(
        "--fixture", fixture("baseline_delivery.json"), "--scope", fixture("scope_manifest.json"),
        "--run-id", "run-1", "--source-ref", "feed-a", "--database", db,
    )
    out = json.loads(run_cli("--expire-source", "feed-a", "--database", db).stdout)
    assert out == {"source_ref": "feed-a", "expired": 4}
    view = json.loads(run_cli("--view", "--database", db).stdout)
    assert all(row["visibility"] == "expired" for row in view)


def test_cli_readme_command_still_works(tmp_path):
    db = str(tmp_path / "catalog.sqlite3")
    out = json.loads(run_cli("--fixture", fixture("supplier_payload.json"), "--database", db).stdout)
    assert out["status"] == "published"
    assert out["processed"] == 1  # both records canonicalize to the same cell
    view = json.loads(run_cli("--view", "--database", db).stdout)
    assert len(view) == 1
    assert view[0]["price"] == "18.75"
    # re-running the identical import is a reported no-op
    out = json.loads(run_cli("--fixture", fixture("supplier_payload.json"), "--database", db).stdout)
    assert out["status"] == "already_published"


def test_cli_scope_requires_explicit_run_id(tmp_path):
    proc = run_cli(
        "--fixture", fixture("partial_delivery.json"), "--scope", fixture("scope_manifest.json"),
        "--database", str(tmp_path / "catalog.sqlite3"), expect=2,
    )
    assert "--run-id is required" in proc.stderr
