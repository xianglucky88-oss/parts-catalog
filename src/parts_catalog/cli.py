from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from .errors import PublicationError
from .pipeline import PartsPipeline
from .sources import JsonFixtureSource, JsonScopeSource
from .storage import PartsStore


def _default_run_id(fixture_path: Path) -> str:
    digest = hashlib.sha1(fixture_path.read_bytes()).hexdigest()[:12]
    return f"{fixture_path.stem}-{digest}"


def _visible_quote_dict(quote) -> dict:
    return {
        "supplier": quote.supplier,
        "canonical_number": quote.canonical_number,
        "currency": quote.currency,
        "price": str(quote.price) if quote.price is not None else None,
        "lead_time_days": quote.lead_time_days,
        "stock_status": quote.stock_status,
        "run_id": quote.run_id,
        "source_ref": quote.source_ref,
        "published_at": quote.published_at,
        "valid_until": quote.valid_until,
        "visibility": quote.visibility,
    }


def _scope_entry_dict(entry) -> dict:
    return {"supplier": entry.supplier, "canonical_number": entry.canonical_number}


def main() -> None:
    parser = argparse.ArgumentParser(prog="parts-catalog")
    parser.add_argument("--fixture", help="supplier delivery payload (JSON list)")
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--scope", help="query manifest pinned for the run; requires --run-id")
    parser.add_argument("--run-id", help="collection run identifier; reuse it to resume an unfinished run")
    parser.add_argument("--source-ref", help="defaults to the fixture path")
    parser.add_argument("--valid-until", help="ISO timestamp after which the published quotes are reported as expired")
    parser.add_argument("--view", action="store_true", help="print the procurement-visible quote view")
    parser.add_argument("--pending", metavar="RUN_ID", help="print the queries still outstanding for a run")
    parser.add_argument("--expire-source", metavar="SOURCE_REF", help="mark every published quote from a source as expired")
    args = parser.parse_args()

    store = PartsStore(args.database)
    try:
        if args.view:
            print(json.dumps([_visible_quote_dict(q) for q in store.visible_quotes()], indent=2))
            return
        if args.pending:
            info = store.run_info(args.pending)
            print(json.dumps({
                "run_id": info.run_id,
                "status": info.status,
                "expected": info.expected_queries,
                "staged": info.staged_queries,
                "pending": [_scope_entry_dict(e) for e in store.pending_queries(args.pending)],
            }, indent=2))
            return
        if args.expire_source:
            count = store.expire_source(args.expire_source)
            print(json.dumps({"source_ref": args.expire_source, "expired": count}))
            return
        if not args.fixture:
            parser.error("--fixture is required unless --view, --pending or --expire-source is used")
        if args.scope and not args.run_id:
            parser.error("--run-id is required when --scope pins the query manifest")
        fixture_path = Path(args.fixture)
        run_id = args.run_id or _default_run_id(fixture_path)
        # A run resumed across several delivery files stays one logical source:
        # the pinned manifest names the feed, not the individual payload file.
        source_ref = args.source_ref or args.scope or str(fixture_path)
        scope = JsonScopeSource(args.scope).read() if args.scope else None
        outcome = PartsPipeline(JsonFixtureSource(fixture_path), store).collect(
            run_id,
            currency=args.currency,
            source_ref=source_ref,
            scope=scope,
            valid_until=args.valid_until,
        )
        print(json.dumps({
            "run_id": outcome.run_id,
            "status": outcome.status,
            "source_ref": outcome.source_ref,
            "processed": outcome.delivered,
            "pending": [_scope_entry_dict(e) for e in outcome.pending],
            "database": args.database,
        }))
    except PublicationError as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
