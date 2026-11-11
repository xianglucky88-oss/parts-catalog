"""演示用子进程 worker：在干净解释器中扫描/摄取来源并报告内存峰值。

以 ``python3 -I`` 启动，通过 argv 接收参数，stdout 只输出一行 JSON。
独立成模块是为了避免在父进程里用字符串拼接可执行代码。

用法：
    scan_worker.py <path> <chunk_kb> scan      # 只做来源层扫描
    scan_worker.py <path> <chunk_kb> pipeline <batch_size> <db_path>
"""
from __future__ import annotations

import json
import resource
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from parts_catalog import (  # noqa: E402
    CheckpointStore,
    IncrementalJsonSource,
    IngestionError,
    PartsPipeline,
    PartsStore,
)


def _rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def run_scan(path: Path, chunk_kb: int) -> dict:
    baseline = _rss_kb()
    count = 0
    try:
        for _ in IncrementalJsonSource(path, chunk_size=chunk_kb * 1024).iter_records():
            count += 1
    except IngestionError as exc:
        return {
            "status": "stopped_at_bad_record",
            "scanned_ok": count,
            "bad_sequence": exc.record_sequence,
            "bad_line": exc.line,
            "bad_offset": exc.offset,
            "peak_rss_kb": _rss_kb(),
            "baseline_rss_kb": baseline,
        }
    return {
        "status": "complete",
        "scanned_ok": count,
        "peak_rss_kb": _rss_kb(),
        "baseline_rss_kb": baseline,
    }


def run_pipeline(path: Path, chunk_kb: int, batch_size: int, db_path: Path) -> dict:
    baseline = _rss_kb()
    cp_store = CheckpointStore(db_path.with_name(db_path.name + ".checkpoint.json"))
    store = PartsStore(db_path)
    store.connection.execute("PRAGMA journal_mode=MEMORY")
    store.connection.execute("PRAGMA synchronous=OFF")
    pipeline = PartsPipeline(
        IncrementalJsonSource(path, chunk_size=chunk_kb * 1024),
        store,
        batch_size=batch_size,
        checkpoint_store=cp_store,
    )
    try:
        stats = pipeline.run_streaming(source_ref=str(path))
        return {
            "status": "complete",
            "processed": stats.records_processed,
            "total": stats.total_records,
            "parts": store.count_parts(),
            "peak_rss_kb": _rss_kb(),
            "baseline_rss_kb": baseline,
        }
    except IngestionError as exc:
        return {
            "status": "stopped_at_bad_record",
            "bad_sequence": exc.record_sequence,
            "bad_line": exc.line,
            "bad_offset": exc.offset,
            "safe_offset": getattr(exc, "safe_offset", None),
            "safe_records": getattr(exc, "safe_records_processed", None),
            "parts": store.count_parts(),
            "peak_rss_kb": _rss_kb(),
            "baseline_rss_kb": baseline,
        }
    finally:
        store.close()


def main() -> int:
    path = Path(sys.argv[1])
    chunk_kb = int(sys.argv[2])
    mode = sys.argv[3]
    if mode == "scan":
        result = run_scan(path, chunk_kb)
    elif mode == "pipeline":
        batch_size = int(sys.argv[4])
        db_path = Path(sys.argv[5])
        result = run_pipeline(path, chunk_kb, batch_size, db_path)
    else:
        raise SystemExit(f"未知模式: {mode}")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
