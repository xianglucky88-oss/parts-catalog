from __future__ import annotations
import argparse
import json
from .normalize import canonical_part_number
from .sources import JsonFixtureSource
from .storage import PartsStore
from .pipeline import PartsPipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", help="supplier payload to import (JSON list)")
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--origin", default="feed", choices=["feed", "manual"],
                        help="origin attributed to records that do not carry their own "
                             "'origin' key; 'manual' marks signed-off master data")
    parser.add_argument("--review", metavar="PART_NUMBER",
                        help="print attribution, adjudication ledger and quotes for one "
                             "catalog part, then exit")
    args = parser.parse_args()
    store = PartsStore(args.database)
    try:
        if args.review:
            report = store.review(canonical_part_number(args.review))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        if not args.fixture:
            parser.error("--fixture is required unless --review is given")
        count = PartsPipeline(JsonFixtureSource(args.fixture), store).run(args.currency, args.fixture, args.origin)
        print({"processed": count, "database": args.database})
    finally:
        store.close()


if __name__ == "__main__":
    main()
