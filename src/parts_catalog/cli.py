from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checkpoint import CheckpointError, CheckpointStore
from .pipeline import PartsPipeline
from .sources import IngestionError, IncrementalJsonSource
from .storage import PartsStore


def _default_checkpoint_path(database: str) -> str:
    db_path = Path(database)
    return str(db_path.with_name(db_path.name + ".checkpoint.json"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="工业备件目录增量摄取")
    parser.add_argument("--fixture", required=True, help="供应商来源文件（JSON 数组或逐行 JSON）")
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--batch-size", type=int, default=500, help="每批落库并记录检查点的记录数")
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="检查点文件路径，默认放在数据库旁（<database>.checkpoint.json）",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="忽略并删除已有检查点，从文件开头重新摄取",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checkpoint_path = args.checkpoint or _default_checkpoint_path(args.database)
    cp_store = CheckpointStore(checkpoint_path)

    if args.reset_checkpoint:
        cp_store.reset()

    try:
        existing = cp_store.load()
    except CheckpointError as exc:
        print(json.dumps(
            {"error": "checkpoint_invalid", "detail": str(exc), "checkpoint": checkpoint_path},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 2

    store = PartsStore(args.database)
    try:
        source = IncrementalJsonSource(args.fixture, checkpoint=existing)
        pipeline = PartsPipeline(
            source,
            store,
            batch_size=args.batch_size,
            checkpoint_store=cp_store,
        )
        stats = pipeline.run_streaming(args.currency, args.fixture)
    except IngestionError as exc:
        # 坏记录不跳过：带着位置和安全恢复起点失败退出。
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "location": exc.location(),
                    "safe_resume_offset": getattr(exc, "safe_offset", source.start_offset if "source" in locals() else 0),
                    "records_safely_processed": getattr(exc, "safe_records_processed", 0),
                    "checkpoint": checkpoint_path,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        store.close()

    print(json.dumps(
        {
            "processed": stats.records_processed,
            "total_records": stats.total_records,
            "batches_committed": stats.batches_committed,
            "total_batches": stats.total_batches,
            "resumed_from_offset": stats.resumed_offset,
            "last_offset": stats.last_offset,
            "database": args.database,
            "checkpoint": stats.checkpoint_path,
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
