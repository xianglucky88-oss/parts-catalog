from __future__ import annotations
import argparse
import json
import sys
from .handoff import EnvelopeError, load_envelope
from .ingest import BatchIngestor
from .sources import JsonFixtureSource
from .storage import PartsStore
from .pipeline import PartsPipeline


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", help="manual import: plain JSON list of supplier parts")
    source.add_argument("--batch", help="collector handoff: batch envelope per contract/batch-envelope.schema.json")
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args(argv)
    store = PartsStore(args.database)
    try:
        if args.fixture:
            count = PartsPipeline(JsonFixtureSource(args.fixture), store).run(args.currency, args.fixture)
            print({"processed": count, "database": args.database})
            return
        try:
            envelope = load_envelope(args.batch)
        except (OSError, EnvelopeError) as exc:
            print(f"invalid batch envelope: {exc}", file=sys.stderr)
            raise SystemExit(1)
        report = BatchIngestor(store).ingest(envelope, args.currency)
        print(json.dumps({**report.as_dict(), "database": args.database}, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
