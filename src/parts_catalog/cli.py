from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import PartsPipeline
from .snapshot import (
    SnapshotError,
    export_snapshot,
    import_snapshot,
    sha256_file,
    state_fingerprint,
    verify_snapshot,
)
from .sources import JsonFixtureSource
from .storage import PartsStore


def _print(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _run_ingest(args: argparse.Namespace) -> dict:
    store = PartsStore(args.database)
    try:
        count = PartsPipeline(
            JsonFixtureSource(args.fixture), store
        ).run(args.currency, args.fixture)
        return {"processed": count, "database": args.database}
    finally:
        store.close()


def _run_export(args: argparse.Namespace) -> dict:
    artifacts = export_snapshot(
        database=args.database,
        output_dir=args.output_dir,
        batch_id=args.batch_id,
        source_ref=args.source_ref,
    )
    return {
        "snapshot_id": artifacts.snapshot_id,
        "batch_id": artifacts.batch_id,
        "bundle": str(artifacts.bundle),
        "sidecar": str(artifacts.sidecar),
        "part_count": artifacts.part_count,
        "quote_count": artifacts.quote_count,
        "bundle_sha256": sha256_file(artifacts.bundle),
    }


def _run_verify(args: argparse.Namespace) -> dict:
    info = verify_snapshot(
        args.bundle, sidecar=args.sidecar, require_sidecar=args.require_sidecar
    )
    return {"status": "valid", **info.summary()}


def _run_import(args: argparse.Namespace) -> dict:
    result = import_snapshot(
        args.bundle,
        args.database,
        sidecar=args.sidecar,
        require_sidecar=args.require_sidecar,
    )
    return {"status": "imported", **result}


def _run_fingerprint(args: argparse.Namespace) -> dict:
    return {"database": args.database, "fingerprint": state_fingerprint(args.database)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts-catalog")
    subparsers = parser.add_subparsers(dest="command")

    ingest = subparsers.add_parser("ingest", help="load a supplier JSON fixture into a database")
    ingest.add_argument("--fixture", required=True)
    ingest.add_argument("--database", default="parts-catalog.sqlite3")
    ingest.add_argument("--currency", default="USD")
    ingest.set_defaults(handler=_run_ingest)

    export = subparsers.add_parser("export-snapshot", help="export a PCSv1 exchange bundle")
    export.add_argument("--database", required=True)
    export.add_argument("--output-dir", required=True)
    export.add_argument("--batch-id", required=True)
    export.add_argument("--source-ref", required=True)
    export.set_defaults(handler=_run_export)

    verify = subparsers.add_parser("verify-snapshot", help="validate a bundle without importing")
    verify.add_argument("bundle")
    verify.add_argument("--sidecar", default=None)
    verify.add_argument("--require-sidecar", action="store_true")
    verify.set_defaults(handler=_run_verify)

    import_parser = subparsers.add_parser("import-snapshot", help="validate and atomically import a bundle")
    import_parser.add_argument("bundle")
    import_parser.add_argument("--database", required=True)
    import_parser.add_argument("--sidecar", default=None)
    import_parser.add_argument("--require-sidecar", action="store_true")
    import_parser.set_defaults(handler=_run_import)

    fingerprint = subparsers.add_parser("fingerprint", help="print the business-row fingerprint of a database")
    fingerprint.add_argument("--database", required=True)
    fingerprint.set_defaults(handler=_run_fingerprint)

    # Legacy positional-less invocation: parts-catalog --fixture ... --database ...
    parser.add_argument("--fixture", help=argparse.SUPPRESS)
    parser.add_argument("--database", default="parts-catalog.sqlite3", help=argparse.SUPPRESS)
    parser.add_argument("--currency", default="USD", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        if args.fixture is None:
            parser.error("a subcommand or --fixture is required")
        handler = _run_ingest
    try:
        _print(handler(args))
        return 0
    except SnapshotError as exc:
        _print({"status": "rejected", **exc.to_dict()})
        return 2
    except (OSError, ValueError) as exc:
        _print({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
