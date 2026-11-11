from __future__ import annotations
import argparse
import sys
from .adapters import SourceBatchRejectedError, default_registry
from .sources import JsonFixtureSource
from .storage import PartsStore
from .pipeline import PartsPipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args()
    store = PartsStore(args.database)
    try:
        pipeline = PartsPipeline(JsonFixtureSource(args.fixture), store, default_registry())
        count = pipeline.run(args.currency, args.fixture)
    except SourceBatchRejectedError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    else:
        print({"processed": count, "database": args.database})
    finally:
        store.close()


if __name__ == "__main__":
    main()
