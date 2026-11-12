from __future__ import annotations
import argparse

from .pipeline import PartsPipeline
from .sources import JsonFixtureSource
from .storage import PartsStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args()
    store = PartsStore(args.database)
    try:
        pipeline = PartsPipeline(JsonFixtureSource(args.fixture), store)
        report = pipeline.run_with_report(args.currency, args.fixture)
        print(
            {
                "processed": len(report.decisions),
                "database": args.database,
                "policy_version": report.policy_version,
                "quality": {
                    key: value
                    for key, value in report.totals.items()
                    if key != "policy_version"
                },
            }
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
