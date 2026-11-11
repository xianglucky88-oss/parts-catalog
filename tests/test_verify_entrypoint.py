"""Offline proof that the data-engineering verification entry point works.

These tests exercise scripts/verify_data_engineering.py end-to-end using only
the bundled fixture: the entry point must be repeatable, must propagate stage
exit codes, must name the failed stage, must never print passwords or tokens,
and must clean up its temporary SQLite workspace.

The tests use only the standard library, so they also run in environments
where pytest itself is not installed yet.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_data_engineering.py"
FIXTURE = REPO_ROOT / "fixtures" / "supplier_payload.json"

spec = importlib.util.spec_from_file_location("verify_data_engineering", SCRIPT)
verify = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify  # dataclasses resolve their module via sys.modules
spec.loader.exec_module(verify)


def run_entry(*extra_args: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra_args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def leftover_workdirs() -> set:
    return set(Path(tempfile.gettempdir()).glob("parts-verify-*"))


def test_entrypoint_succeeds_and_cleans_up():
    before = leftover_workdirs()
    proc = run_entry()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT: OK" in proc.stdout
    for stage in ("deps", "python-pipeline", "node-dry-run", "cleanup"):
        assert f"[verify][{stage}]" in proc.stdout
    assert leftover_workdirs() - before == set(), "temporary SQLite workspace was not cleaned up"


def test_entrypoint_is_repeatable_with_offline_fixture():
    fixture_bytes = FIXTURE.read_bytes()
    first = run_entry()
    second = run_entry()
    assert first.returncode == 0, first.stdout
    assert second.returncode == 0, second.stdout
    assert "idempotent" in second.stdout  # python stage ran the pipeline twice
    assert FIXTURE.read_bytes() == fixture_bytes, "offline fixture must not be modified"


def test_python_stage_failure_propagates_exit_code_and_is_locatable(tmp_path):
    bad_fixture = tmp_path / "bad_fixture.json"
    bad_fixture.write_text(
        '[{"supplier": "S", "part_number": "X-1", "brand": "B", "price": "not-a-price"}]',
        encoding="utf-8",
    )
    before = leftover_workdirs()
    proc = run_entry("--fixture", str(bad_fixture))
    assert proc.returncode == 1, proc.stdout  # the Python CLI's exit code, propagated
    assert "[verify][python-pipeline] STAGE FAILED" in proc.stdout
    assert "stage 'python-pipeline'" in proc.stdout
    assert "[verify][cleanup]" in proc.stdout  # cleanup still ran after the failure
    assert leftover_workdirs() - before == set(), "temporary SQLite workspace was not cleaned up"


def test_missing_fixture_fails_python_stage():
    proc = run_entry("--fixture", "/nonexistent/supplier_payload.json")
    assert proc.returncode == 1
    assert "stage 'python-pipeline'" in proc.stdout


def test_output_never_contains_passwords_or_tokens():
    canaries = {
        "PORTAL_PASSWORD": "pw-canary-7f3c9d2e",
        "PORTAL_URL": "https://ci-user:pw-canary-7f3c9d2e@portal.example.internal",
        "SUPPLIER_API_TOKEN": "tok-canary-51f0ab94",
    }
    proc = run_entry(env_overrides=canaries)
    assert proc.returncode == 0, proc.stdout
    combined = proc.stdout + proc.stderr
    assert "pw-canary-7f3c9d2e" not in combined
    assert "tok-canary-51f0ab94" not in combined
    # the dry-run echoes the portal URL; the embedded password must be masked
    assert "https://ci-user:***@portal.example.internal" in combined


def test_redact_masks_secret_values():
    secrets = ["s3cr3t", "tok-123"]
    assert verify.redact("password=s3cr3t token=tok-123", secrets) == "password=*** token=***"
    assert verify.redact("https://u:s3cr3t@host", secrets) == "https://u:***@host"
    assert verify.redact("nothing to hide", secrets) == "nothing to hide"


def test_collect_secrets_picks_passwords_tokens_and_url_credentials():
    env = {
        "PORTAL_PASSWORD": "pw",
        "APP_API_TOKEN": "tok",
        "EDITOR": "vim",
        "PORTAL_URL": "https://user:url-pw@host",
    }
    found = verify.collect_secrets(env)
    assert "pw" in found
    assert "tok" in found
    assert "url-pw" in found  # credential embedded in a URL
    assert "vim" not in found
