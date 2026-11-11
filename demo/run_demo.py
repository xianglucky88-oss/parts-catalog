#!/usr/bin/env python3
"""受控大样本演示：增量摄取的内存有界性、坏记录定位与检查点恢复。

用法（在仓库根目录执行）：
    python3 -I demo/run_demo.py                 # 默认 120MB JSONL
    python3 -I demo/run_demo.py --mb 250        # 自定义样本大小
    python3 -I demo/run_demo.py --keep          # 保留生成的数据目录

脚本与数据严格隔离：所有大文件生成在独立临时目录中，本脚本只用标准库，
并以 -I 运行，避免来源目录中的同名模块污染导入。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from parts_catalog import (  # noqa: E402
    CheckpointStore,
    IncrementalJsonSource,
    PartsPipeline,
    PartsStore,
    RecordParseError,
)


# --------------------------------------------------------------------- 数据生成
def _row(seq: int) -> dict:
    return {
        "supplier": "Northstar Ltd",
        "part_number": f"PN-{seq:08d}",
        "brand": "Acme Inc.",
        "description": f"深沟球密封轴承 {seq} " + "耐磨合金钢 " * 6,
        "unit": "EA",
        "package_quantity": 1,
        "lead_time_days": seq % 21,
        "stock_status": "In Stock" if seq % 3 else "Backorder",
        "price": f"{(seq % 500) + 10}.{seq % 100:02d}",
    }


def generate_jsonl(path: Path, target_mb: float, bad_index: int) -> tuple[int, int]:
    """生成 JSONL 大样本，在文件正中写入一条规范化必失败的记录。

    返回 ``(总记录数, 坏记录序号)``。先用一条样本估算记录平均字节数，
    再流式写盘；坏索引取 ``min(bad_index, 总条数//2)``，保证坏记录始终
    落在文件中部而不是末尾，且全程不把样本保存在内存里。
    """
    target_bytes = int(target_mb * 1024 * 1024)
    sample_line = json.dumps(_row(10_000), ensure_ascii=False) + "\n"
    approx_line_bytes = len(sample_line.encode("utf-8"))
    estimated_total = max(2, target_bytes // approx_line_bytes)
    actual_bad = min(bad_index, estimated_total // 2)

    written = 0
    seq = 0
    with path.open("w", encoding="utf-8") as handle:
        while written < target_bytes:
            row = _row(seq)
            if seq == actual_bad:
                row["price"] = "not-a-price"  # 等长可修复的坏值
            line = json.dumps(row, ensure_ascii=False) + "\n"
            handle.write(line)
            written += len(line.encode("utf-8"))
            seq += 1
    return seq, actual_bad


def generate_array(path: Path, rows: int) -> None:
    """生成同样记录的 JSON 数组样本，证明两种格式共用同一套摄取机制。"""
    with path.open("w", encoding="utf-8") as handle:
        handle.write("[\n")
        for seq in range(rows):
            prefix = "  " if seq == 0 else ",\n  "
            handle.write(prefix + json.dumps(_row(seq), ensure_ascii=False))
        handle.write("\n]\n")


# --------------------------------------------------------------------- 内存测量
def _peak_rss_mb() -> float:
    # Linux ru_maxrss 单位为 KB
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def scan_pure(path: Path, chunk_kb: int) -> dict:
    """只做来源层词法扫描（不 json.loads 业务字段、不入库），佐证扫描本身有界。"""
    from parts_catalog.sources import _scan_top_level_values, detect_format

    baseline = _peak_rss_mb()
    count = 0
    started = time.perf_counter()
    for _raw, start, _end, _line, _col in _scan_top_level_values(
        path, detect_format(path), 0, chunk_kb * 1024
    ):
        count += 1
    return {
        "records_lexed": count,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "peak_rss_mb": round(_peak_rss_mb(), 2),
        "delta_mb": round(_peak_rss_mb() - baseline, 2),
    }


def run_pipeline(path: Path, workdir: Path, batch_size: int) -> dict:
    """完整管道运行，自动沿用工作目录中的检查点，返回统计或结构化错误。"""
    db_path = workdir / "parts.sqlite3"
    cp_store = CheckpointStore(workdir / "checkpoint.json")
    existing = cp_store.load()
    store = PartsStore(db_path)
    store.connection.execute("PRAGMA journal_mode=MEMORY")
    store.connection.execute("PRAGMA synchronous=OFF")
    pipeline = PartsPipeline(
        IncrementalJsonSource(path, checkpoint=existing),
        store,
        batch_size=batch_size,
        checkpoint_store=cp_store,
    )
    started = time.perf_counter()
    try:
        stats = pipeline.run_streaming(currency="USD", source_ref=str(path))
        return {
            "status": "complete",
            "stats": stats,
            "parts_in_db": store.count_parts(),
            "elapsed_s": round(time.perf_counter() - started, 2),
        }
    except RecordParseError as exc:
        return {
            "status": "stopped_at_bad_record",
            "bad_sequence": exc.record_sequence,
            "bad_line": exc.line,
            "bad_offset": exc.offset,
            "safe_resume_offset": exc.safe_offset,
            "records_safely_processed": exc.safe_records_processed,
            "parts_in_db": store.count_parts(),
            "checkpoint": cp_store.load(),
            "elapsed_s": round(time.perf_counter() - started, 2),
        }
    finally:
        store.close()


def repair_equal_length(path: Path) -> None:
    """等长字节修复坏价格，保证所有后续记录的字节偏移不变。"""
    old = b'"price": "not-a-price"'
    replacement = b'"price": "29.90"'
    new = replacement + b" " * (len(old) - len(replacement))
    assert len(old) == len(new)
    data = path.read_bytes()
    assert data.count(old) == 1, "坏记录片段应当只出现一次"
    path.write_bytes(data.replace(old, new))


# --------------------------------------------------------------------- 子进程隔离的内存对照
def scan_in_subprocess(path: Path, chunk_kb: int, mode: str = "pipeline",
                       batch_size: int = 2000, db_path: Path | None = None) -> dict:
    """在全新解释器进程中运行，得到不受前序对象污染的干净 RSS 基线。"""
    worker = REPO_ROOT / "demo" / "scan_worker.py"
    cmd = [sys.executable, "-I", str(worker), str(path), str(chunk_kb), mode]
    if mode == "pipeline":
        cmd += [str(batch_size), str(db_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout.strip())


# --------------------------------------------------------------------- 主流程
def main() -> int:
    parser = argparse.ArgumentParser(description="增量摄取受控大样本演示")
    parser.add_argument("--mb", type=float, default=120.0, help="大样本目标大小（MB）")
    parser.add_argument("--small-mb", type=float, default=8.0, help="内存对照用小样本（MB）")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--chunk-kb", type=int, default=64)
    parser.add_argument("--keep", action="store_true", help="保留生成的数据目录")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="parts-demo-"))
    print(f"== 数据目录（隔离）: {workdir}")
    try:
        large = workdir / "catalog_large.jsonl"
        small = workdir / "catalog_small.jsonl"
        array_sample = workdir / "catalog_array.json"

        print(f"\n[1/7] 生成受控大样本，目标 {args.mb} MB，坏记录位于正中 ...")
        total, bad_index = generate_jsonl(large, args.mb, bad_index=50_000)
        size_mb = large.stat().st_size / 1024 / 1024
        print(f"      大样本: {total} 条记录, {size_mb:.1f} MB, 坏记录序号={bad_index}")
        small_total, small_bad = generate_jsonl(small, args.small_mb, bad_index=2_000)
        print(f"      小样本: {small_total} 条记录, {small.stat().st_size / 1024 / 1024:.1f} MB, "
              f"坏记录序号={small_bad}")
        array_rows = min(20_000, total)
        generate_array(array_sample, array_rows)
        print(f"      数组样本: {array_rows} 条记录, {array_sample.stat().st_size / 1024 / 1024:.1f} MB")

        print("\n[2/7] 内存有界性对照（独立子进程，相同分块，不同文件大小）...")
        small_db = workdir / "mem_small.sqlite3"
        large_db = workdir / "mem_large.sqlite3"
        small_measure = scan_in_subprocess(
            small, args.chunk_kb, "pipeline", args.batch_size, small_db
        )
        large_measure = scan_in_subprocess(
            large, args.chunk_kb, "pipeline", args.batch_size, large_db
        )
        assert small_measure["status"] == "stopped_at_bad_record"
        assert large_measure["status"] == "stopped_at_bad_record"
        print(f"      小样本 {small.stat().st_size // 1024} KB -> 峰值 RSS "
              f"{small_measure['peak_rss_kb'] / 1024:.2f} MB，停在第 "
              f"{small_measure['bad_sequence']} 条")
        print(f"      大样本 {large.stat().st_size // 1024 // 1024} MB -> 峰值 RSS "
              f"{large_measure['peak_rss_kb'] / 1024:.2f} MB，停在第 "
              f"{large_measure['bad_sequence']} 条")
        ratio = large_measure["peak_rss_kb"] / small_measure["peak_rss_kb"]
        size_ratio = large.stat().st_size / small.stat().st_size
        bounded = ratio < 1.5
        print(f"      文件放大 {size_ratio:.1f} 倍，峰值内存仅 {ratio:.2f} 倍 -> "
              f"{'有界 ✓' if bounded else '异常 ✗'}")
        assert bounded

        print("\n[3/7] 坏记录精确定位（不跳过，直接失败）...")
        lex = scan_pure(large, args.chunk_kb)
        print(f"      纯词法扫描 {lex['records_lexed']} 条记录，增量 RSS={lex['delta_mb']} MB，"
              f"耗时={lex['elapsed_s']}s")
        locate = scan_in_subprocess(
            large, args.chunk_kb, "pipeline", args.batch_size,
            workdir / "locate.sqlite3",
        )
        print(f"      完整管道在坏记录处中止：")
        print(f"      记录序号={locate['bad_sequence']} 行号={locate['bad_line']} "
              f"字节偏移={locate['bad_offset']}")
        print(f"      安全恢复起点={locate['safe_offset']} "
              f"已安全处理={locate['safe_records']} 条，已落库 {locate['parts']} 行")
        assert locate["bad_sequence"] == bad_index
        assert locate["parts"] == locate["safe_records"]

        print(f"\n[4/7] 端到端管道（批次大小 {args.batch_size}），预期在坏记录处中止 ...")
        run1 = run_pipeline(large, workdir, args.batch_size)
        assert run1["status"] == "stopped_at_bad_record"
        cp = run1["checkpoint"]
        print(f"      坏记录: 序号={run1['bad_sequence']} 行={run1['bad_line']} 偏移={run1['bad_offset']}")
        print(f"      安全恢复起点: 偏移={run1['safe_resume_offset']} "
              f"已安全处理={run1['records_safely_processed']} 条")
        print(f"      检查点: next_offset={cp.next_offset} records={cp.records_processed} "
              f"batches={cp.batch_count}")
        print(f"      SQLite 已落库 {run1['parts_in_db']} 条（== 检查点记录数，无在途泄漏）")
        assert run1["parts_in_db"] == cp.records_processed

        print("\n[5/7] 等长字节修复坏记录（后续偏移保持不变）...")
        repair_equal_length(large)
        recs = list(IncrementalJsonSource(large).iter_records())
        print(f"      修复后全文件可解析，共 {len(recs)} 条；"
              f"原坏记录位置价格={recs[bad_index].data['price']}")
        assert len(recs) == total

        print("\n[6/7] 从检查点恢复，只处理剩余记录 ...")
        db_path = workdir / "parts.sqlite3"
        cp_store = CheckpointStore(workdir / "checkpoint.json")
        store = PartsStore(db_path)
        store.connection.execute("PRAGMA journal_mode=MEMORY")
        store.connection.execute("PRAGMA synchronous=OFF")
        stats = PartsPipeline(
            IncrementalJsonSource(large, checkpoint=cp_store.load()),
            store,
            batch_size=args.batch_size,
            checkpoint_store=cp_store,
        ).run_streaming(source_ref=str(large))
        print(f"      本次处理={stats.records_processed} 累计={stats.total_records} "
              f"本次批次={stats.batches_committed} 累计批次={stats.total_batches}")
        print(f"      数据库 parts 行数={store.count_parts()}（应等于总记录数 {total}）")
        assert store.count_parts() == total
        final_cp = cp_store.load()
        assert final_cp.records_processed == total
        store.close()

        print("\n[7/7] 数组格式样本走同一套机制 + 重复运行幂等 ...")
        array_dir = workdir / "array_run"
        array_dir.mkdir()
        array_cp = CheckpointStore(array_dir / "cp.json")
        store = PartsStore(array_dir / "a.db")
        astats = PartsPipeline(
            IncrementalJsonSource(array_sample), store,
            batch_size=args.batch_size, checkpoint_store=array_cp,
        ).run_streaming()
        print(f"      数组样本: 处理 {astats.total_records} 条, 格式={array_cp.load().file_format}, "
              f"落库 {store.count_parts()} 行")
        store.close()
        rerun = run_pipeline(large, workdir, args.batch_size)
        print(f"      大样本重跑: status={rerun['status']}（检查点已在末尾，零新增）")
        assert rerun["status"] == "complete"
        assert rerun["stats"].records_processed == 0

        print("\n== 结论 ==")
        print("  1. 峰值内存由分块大小决定，不随文件体积增长（见 [2/7]）")
        print("  2. 坏记录带序号/行号/字节偏移精确失败，绝不静默跳过（见 [3/7][4/7]）")
        print("  3. 检查点记录安全偏移、记录数与批次数，修复后从该点恢复且恰好一次（见 [6/7]）")
        return 0
    finally:
        if args.keep:
            print(f"\n数据已保留: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
