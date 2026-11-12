from __future__ import annotations

import argparse

from .pipeline import PartsPipeline
from .sources import JsonFixtureSource
from .storage import PartsStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", help="供应商 JSON 报价载荷")
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    parser.add_argument(
        "--admit-existing",
        action="store_true",
        help="对数据库中迁移前已存在的 quotes 记录执行准入留痕（可重复执行）",
    )
    args = parser.parse_args()
    store = PartsStore(args.database)
    try:
        if args.admit_existing:
            summary = store.admit_existing_quotes()
            print({"migration": summary, "database": args.database})
            return
        if not args.fixture:
            parser.error("--fixture is required unless --admit-existing is used")
        summary = PartsPipeline(JsonFixtureSource(args.fixture), store).run(args.currency, args.fixture)
        print(
            {
                "processed": summary["processed"],
                "admitted": summary["admitted"],
                "rejected": summary["rejected"],
                "database": args.database,
            }
        )
        for decision in summary["decisions"]:
            status = "ADMITTED" if decision.admitted else f"REJECTED[{decision.reason_code}]"
            print(
                f"  {status}: raw_price={decision.raw_amount!r} raw_currency={decision.raw_currency!r}"
                + ("" if decision.admitted else f" -> {decision.reason}")
            )
    finally:
        store.close()


if __name__ == "__main__":
    main()
