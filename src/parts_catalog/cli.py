from __future__ import annotations
import argparse
import json
import sys

from .audit import entity_trail, verify, verify_input
from .sources import JsonFixtureSource
from .storage import PartsStore, quote_entity_key
from .pipeline import PartsPipeline


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_import(args: argparse.Namespace) -> int:
    store = PartsStore(args.database)

    def _boom(_store: PartsStore) -> None:
        raise RuntimeError("simulated failure before commit")

    try:
        pipeline = PartsPipeline(
            JsonFixtureSource(args.fixture), store,
            fault_hook=_boom if args.simulate_fault else None,
        )
        result = pipeline.run(args.currency, args.fixture)
        _print(
            {
                "processed": result.record_count,
                "entries_written": result.entries_written,
                "replayed": result.replayed,
                "run_id": result.run_id,
                "input_hash": result.input_hash,
                "database": args.database,
            }
        )
        return 0
    except RuntimeError as exc:
        # The pipeline has already rolled the whole transaction back; surface
        # the failure explicitly so an interrupted import never looks like a
        # completed one.
        _print({"import": "rolled_back", "reason": str(exc), "database": args.database})
        return 4
    finally:
        store.close()


def _cmd_audit(args: argparse.Namespace) -> int:
    store = PartsStore(args.database)
    try:
        if args.fixture:
            raw_records = JsonFixtureSource(args.fixture).read()
            report = verify_input(store, raw_records)
        else:
            report = verify(store)
        _print(report.to_dict())
        return 0 if report.ok else 2
    finally:
        store.close()


def _cmd_trail(args: argparse.Namespace) -> int:
    store = PartsStore(args.database)
    try:
        if args.type == "quote" and args.canonical and args.supplier and args.source_ref:
            key = quote_entity_key(args.canonical, args.supplier, args.source_ref)
        else:
            key = args.key
        if not key:
            raise SystemExit("trail requires --key, or --canonical/--supplier/--source-ref for quotes")
        trail = entity_trail(store, args.type, key)
        _print({"entity_type": args.type, "entity_key": key, "events": len(trail), "trail": trail})
        return 0 if trail else 3
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts_catalog")
    sub = parser.add_subparsers(dest="command")

    p_import = sub.add_parser("import", help="run an import with evidence ledger writes")
    p_import.add_argument("--fixture", required=True)
    p_import.add_argument("--database", default="parts-catalog.sqlite3")
    p_import.add_argument("--currency", default="USD")
    p_import.add_argument(
        "--simulate-fault",
        action="store_true",
        help="raise after all writes but before commit; the whole transaction must roll back",
    )
    p_import.set_defaults(func=_cmd_import)

    p_audit = sub.add_parser("audit", help="verify ledger integrity and locate tampering")
    p_audit.add_argument("--database", default="parts-catalog.sqlite3")
    p_audit.add_argument("--fixture", help="optional input file to re-bind against stored raw hashes")
    p_audit.set_defaults(func=_cmd_audit)

    p_trail = sub.add_parser("trail", help="show the ordered evidence trail of one entity")
    p_trail.add_argument("--database", default="parts-catalog.sqlite3")
    p_trail.add_argument("--type", required=True, choices=["part", "quote"])
    p_trail.add_argument("--key", help="entity key (canonical number, or canonical|supplier|source_ref)")
    p_trail.add_argument("--canonical")
    p_trail.add_argument("--supplier")
    p_trail.add_argument("--source-ref")
    p_trail.set_defaults(func=_cmd_trail)
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatibility: the historical form had no subcommand.
    if argv and argv[0] not in {"import", "audit", "trail", "-h", "--help"}:
        argv = ["import", *argv]
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(1)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
