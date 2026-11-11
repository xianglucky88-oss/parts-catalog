from __future__ import annotations
import argparse
import json
from .decision import build_decision_projection, project_to_file
from .sources import JsonFixtureSource
from .storage import PartsStore
from .pipeline import PartsPipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", help="supplier payload to import into the catalog")
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    parser.add_argument(
        "--project",
        action="store_true",
        help="emit the deterministic quote decision projection for the stored data",
    )
    parser.add_argument("--output", help="write the projection JSON to this path instead of stdout")
    args = parser.parse_args()

    if not args.fixture and not args.project:
        parser.error("at least one of --fixture (import) or --project (decision projection) is required")

    if args.fixture:
        # Import path is unchanged: fixture -> normalization -> PartsStore.
        store = PartsStore(args.database)
        try:
            count = PartsPipeline(JsonFixtureSource(args.fixture), store).run(args.currency, args.fixture)
            print({"processed": count, "database": args.database})
        finally:
            store.close()

    if args.project:
        if args.output:
            projection = project_to_file(args.database, args.output, args.currency)
            print({"projected": len(projection["decisions"]), "output": args.output})
        else:
            projection = build_decision_projection(args.database, args.currency)
            print(json.dumps(projection, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
