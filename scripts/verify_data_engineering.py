#!/usr/bin/env python3
"""Unified data-engineering verification entry point.

One command orchestrates the full offline verification of the data
engineering pipeline, in four stages:

  stage 1  deps             prepare Python and Node.js dependencies
  stage 2  python-pipeline  run the parts_catalog pipeline against the offline
                            fixture (twice, proving idempotence) and validate
                            the resulting SQLite database
  stage 3  node-dry-run     run the supplier-portal automation in --dry-run mode
  stage 4  cleanup          delete the temporary SQLite workspace (always runs,
                            even when an earlier stage failed)

Guarantees:

  * Exit-code propagation: the process exits with the failing stage's exit
    code. Child exit codes are passed through; signal deaths map to 128+N;
    127 means a required command was not found, 124 a command timeout and
    2 an unmet runtime prerequisite.
  * Locatable failures: every output line is tagged with its stage, and the
    final summary names the failed stage.
  * No secrets in logs: values of password/token/key-style environment
    variables, plus credentials embedded in URLs, are masked as *** before
    any child output is forwarded.
  * Repeatable offline: only the bundled fixture is used, so the entry point
    can be run any number of times and leaves no temporary files behind.

Usage:
    python3 scripts/verify_data_engineering.py [--fixture PATH] [--keep-workdir]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
AUTOMATION_DIR = REPO_ROOT / "automation"
DEFAULT_FIXTURE = REPO_ROOT / "fixtures" / "supplier_payload.json"
WORKDIR_PREFIX = "parts-verify-"

MIN_PYTHON = (3, 9)  # pyproject.toml: requires-python = ">=3.9"
MIN_NODE_MAJOR = 20  # automation/package.json: engines.node = ">=20"
MASK = "***"
COMMAND_TIMEOUT_SECONDS = 600

# Set in the environment of every child this entry point spawns, so that a
# nested invocation (e.g. the pytest stage running tests that themselves run
# this entry point) can detect it and skip recursive test runs.
NESTED_GUARD = "PARTS_VERIFY_ENTRY_ACTIVE"

SENSITIVE_NAME = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|BEARER",
    re.IGNORECASE,
)
URLISH_NAME = re.compile(r"URL|URI|DSN", re.IGNORECASE)


def collect_secrets(env: Mapping[str, str]) -> list[str]:
    """Values that must never appear in logs, longest first.

    Covers password/token/key-style variables and passwords embedded in URLs
    (``https://user:password@host``). Redaction errs on the safe side: any
    non-empty value of a sensitively named variable is masked everywhere.
    """
    secrets: set[str] = set()
    for name, value in env.items():
        if not value:
            continue
        if SENSITIVE_NAME.search(name):
            secrets.add(value)
        if URLISH_NAME.search(name):
            try:
                parsed = urlparse(value)
            except ValueError:
                continue
            if parsed.password:
                secrets.add(parsed.password)
    return sorted(secrets, key=len, reverse=True)


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


class Reporter:
    """Stage-tagged, secret-redacting logger."""

    def __init__(self, secrets: Sequence[str]) -> None:
        self.secrets = list(secrets)

    def log(self, stage: str, message: str) -> None:
        text = redact(str(message), self.secrets)
        for line in text.splitlines() or [""]:
            print(f"[verify][{stage}] {line}", flush=True)


def normalize_exit_code(returncode: int) -> int:
    """Map a child returncode to a propagatable process exit code."""
    if returncode < 0:  # terminated by signal N
        return 128 + (-returncode)
    return returncode or 1


class StageFailure(Exception):
    """A stage failed; carries the exit code the entry point must propagate."""

    def __init__(self, stage: str, exit_code: int, detail: str) -> None:
        super().__init__(f"{stage}: {detail} (exit {exit_code})")
        self.stage = stage
        self.exit_code = exit_code
        self.detail = detail


@dataclass
class Context:
    fixture: Path
    workdir: Path
    keep_workdir: bool
    reporter: Reporter

    @property
    def database(self) -> Path:
        return self.workdir / "parts-catalog-verify.sqlite3"


def python_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    env[NESTED_GUARD] = "1"
    return env


def run_command(
    ctx: Context,
    stage: str,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a child process and forward its output, redacted and stage-tagged."""
    command = [str(arg) for arg in argv]
    ctx.reporter.log(stage, f"$ {shlex.join(command)}")
    try:
        proc = subprocess.run(
            command,
            cwd=cwd or REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise StageFailure(stage, 127, f"command not found: {command[0]}") from None
    except subprocess.TimeoutExpired:
        raise StageFailure(stage, 124, f"command timed out after {COMMAND_TIMEOUT_SECONDS}s") from None
    for line in proc.stdout.splitlines():
        ctx.reporter.log(stage, f"stdout| {line}")
    for line in proc.stderr.splitlines():
        ctx.reporter.log(stage, f"stderr| {line}")
    return proc


def expect_success(stage: str, proc: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    if proc.returncode != 0:
        raise StageFailure(stage, normalize_exit_code(proc.returncode), f"command exited with code {proc.returncode}")
    return proc


def stage_deps(ctx: Context) -> None:
    stage = "deps"
    if sys.version_info < MIN_PYTHON:
        raise StageFailure(
            stage, 2, f"python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} required, found {platform.python_version()}"
        )
    ctx.reporter.log(stage, f"python {platform.python_version()} satisfies requires-python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")

    pip_probe = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
    if pip_probe.returncode == 0:
        ctx.reporter.log(stage, "pip available; installing project with test extras")
        expect_success(stage, run_command(ctx, stage, [sys.executable, "-m", "pip", "install", "-q", "-e", ".[test]"]))
    else:
        ctx.reporter.log(stage, "pip module unavailable; skipping editable install (project has no runtime dependencies)")
    expect_success(stage, run_command(ctx, stage, [sys.executable, "-c", "import parts_catalog"], env=python_env()))
    ctx.reporter.log(stage, "import parts_catalog: OK")

    node = expect_success(stage, run_command(ctx, stage, ["node", "--version"]))
    version = node.stdout.strip()
    match = re.match(r"v?(\d+)", version)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        raise StageFailure(stage, 2, f"node >= {MIN_NODE_MAJOR} required, found {version!r}")
    ctx.reporter.log(stage, f"node {version} satisfies engines >= {MIN_NODE_MAJOR}")

    marker = AUTOMATION_DIR / "node_modules" / "playwright" / "package.json"
    if marker.exists():
        ctx.reporter.log(stage, "node dependencies already prepared (automation/node_modules present); skipping npm install")
        return
    npm = shutil.which("npm")
    if npm is None:
        raise StageFailure(stage, 127, "npm not found and automation/node_modules is missing")
    expect_success(
        stage,
        run_command(
            ctx,
            stage,
            [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock"],
            cwd=AUTOMATION_DIR,
        ),
    )
    ctx.reporter.log(stage, "node dependencies installed (--ignore-scripts: playwright browser download skipped; dry-run needs no browser)")


def database_snapshot(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            "parts": connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0],
            "quotes": connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
        }
    finally:
        connection.close()


def validate_database(database: Path, expected_records: int) -> list[str]:
    """Data-engineering invariants on the pipeline's SQLite output."""
    problems: list[str] = []
    connection = sqlite3.connect(database)
    try:
        parts = connection.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        quotes = connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        if expected_records and parts == 0:
            problems.append("parts table is empty")
        if expected_records and quotes == 0:
            problems.append("quotes table is empty")
        empty_keys = connection.execute("SELECT COUNT(*) FROM parts WHERE canonical_number = ''").fetchone()[0]
        if empty_keys:
            problems.append(f"{empty_keys} part row(s) with empty canonical_number")
        orphan_quotes = connection.execute(
            "SELECT COUNT(*) FROM quotes q LEFT JOIN parts p"
            " ON p.canonical_number = q.canonical_number"
            " WHERE p.canonical_number IS NULL"
        ).fetchone()[0]
        if orphan_quotes:
            problems.append(f"{orphan_quotes} quote row(s) reference a missing part")
    finally:
        connection.close()
    return problems


def stage_python_pipeline(ctx: Context) -> None:
    stage = "python-pipeline"
    try:
        records = json.loads(ctx.fixture.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StageFailure(stage, 1, f"cannot read fixture {ctx.fixture}: {exc}") from None
    except ValueError as exc:
        raise StageFailure(stage, 1, f"fixture {ctx.fixture} is not valid JSON: {exc}") from None
    if not isinstance(records, list):
        raise StageFailure(stage, 1, f"fixture {ctx.fixture} must contain a JSON array")

    command = [
        sys.executable,
        "-m",
        "parts_catalog.cli",
        "--fixture",
        str(ctx.fixture),
        "--database",
        str(ctx.database),
    ]
    snapshot: dict[str, int] | None = None
    for run_number in (1, 2):
        expect_success(stage, run_command(ctx, stage, command, env=python_env()))
        current = database_snapshot(ctx.database)
        ctx.reporter.log(stage, f"pipeline run {run_number}: parts={current['parts']} quotes={current['quotes']}")
        if snapshot is None:
            snapshot = current
        elif current != snapshot:
            raise StageFailure(stage, 1, f"pipeline is not idempotent: run 1 gave {snapshot}, run {run_number} gave {current}")

    problems = validate_database(ctx.database, expected_records=len(records))
    if problems:
        raise StageFailure(stage, 1, "database validation failed: " + "; ".join(problems))
    ctx.reporter.log(stage, f"validated {len(records)} fixture records against SQLite output; invariants hold and re-run is idempotent")

    if os.environ.get(NESTED_GUARD) == "1":
        ctx.reporter.log(stage, "nested invocation detected; skipping unit tests to avoid recursion")
    elif importlib.util.find_spec("pytest") is not None:
        # --ignore: the entry-point tests run this script end-to-end; they must
        # not be re-run from inside the entry point itself.
        expect_success(
            stage,
            run_command(
                ctx,
                stage,
                [sys.executable, "-m", "pytest", "-q", "tests", "--ignore=tests/test_verify_entrypoint.py"],
                env=python_env(),
            ),
        )
    else:
        ctx.reporter.log(stage, "pytest not installed; skipping unit tests (install the .[test] extra to enable)")


def stage_node_dry_run(ctx: Context) -> None:
    stage = "node-dry-run"
    env = dict(os.environ)
    # Offline-safe placeholders. Real CI secrets from the environment win and
    # are masked in all forwarded output by the Reporter.
    env.setdefault("PORTAL_URL", "https://portal.example.invalid")
    env.setdefault("PORTAL_USERNAME", "ci-dry-run")
    env.setdefault("PORTAL_PASSWORD", "ci-dry-run-placeholder")
    env.setdefault("PORTAL_QUERY", "AB1204")

    proc = expect_success(
        stage,
        run_command(ctx, stage, ["node", str(AUTOMATION_DIR / "portal_runner.mjs"), "--dry-run"], env=env),
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("mode") != "dry-run":
        raise StageFailure(stage, 1, "portal runner did not confirm dry-run mode")
    ctx.reporter.log(stage, "portal runner confirmed dry-run mode (no browser, no network, no credentials used)")


def stage_cleanup(ctx: Context) -> None:
    stage = "cleanup"
    if ctx.keep_workdir:
        ctx.reporter.log(stage, f"--keep-workdir set; leaving temporary workspace {ctx.workdir} in place")
        return
    if not ctx.workdir.exists():
        ctx.reporter.log(stage, "temporary workspace already absent; nothing to clean")
        return
    try:
        shutil.rmtree(ctx.workdir)
    except OSError as exc:
        raise StageFailure(stage, 1, f"failed to remove temporary workspace {ctx.workdir}: {exc}") from None
    ctx.reporter.log(stage, f"removed temporary SQLite workspace {ctx.workdir}")


STAGES = (
    ("deps", stage_deps),
    ("python-pipeline", stage_python_pipeline),
    ("node-dry-run", stage_node_dry_run),
    ("cleanup", stage_cleanup),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unified data-engineering verification entry point (offline, repeatable)."
    )
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_FIXTURE),
        help="offline supplier payload fixture (default: %(default)s)",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep the temporary SQLite workspace (disables the cleanup stage)",
    )
    args = parser.parse_args(argv)

    reporter = Reporter(collect_secrets(os.environ))
    ctx = Context(
        fixture=Path(args.fixture).expanduser().resolve(),
        workdir=Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX)),
        keep_workdir=args.keep_workdir,
        reporter=reporter,
    )

    started = time.monotonic()
    failure: StageFailure | None = None
    total = len(STAGES)
    for index, (name, stage_fn) in enumerate(STAGES, start=1):
        if failure is not None and name != "cleanup":
            reporter.log(name, f"--- stage {index}/{total} skipped: earlier stage '{failure.stage}' failed ---")
            continue
        reporter.log(name, f"=== stage {index}/{total} start ===")
        stage_started = time.monotonic()
        try:
            stage_fn(ctx)
        except StageFailure as exc:
            reporter.log(name, f"STAGE FAILED (exit {exc.exit_code}): {exc.detail}")
            if failure is None:
                failure = exc
        except Exception:
            reporter.log(name, "STAGE FAILED (exit 1): unexpected orchestrator error:\n" + traceback.format_exc())
            if failure is None:
                failure = StageFailure(name, 1, "unexpected orchestrator error")
        else:
            reporter.log(name, f"stage OK ({time.monotonic() - stage_started:.1f}s)")

    if not ctx.keep_workdir:  # backstop: never leave scratch SQLite files behind
        shutil.rmtree(ctx.workdir, ignore_errors=True)

    elapsed = time.monotonic() - started
    if failure is None:
        reporter.log("summary", f"RESULT: OK - all {total} stages passed in {elapsed:.1f}s")
        return 0
    reporter.log("summary", f"RESULT: FAILED - stage '{failure.stage}' exit code {failure.exit_code}")
    return failure.exit_code


if __name__ == "__main__":
    sys.exit(main())
