"""管道分批、检查点原子写入、坏记录不跳过与端到端恢复测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parts_catalog import (  # noqa: E402
    CheckpointError,
    CheckpointStore,
    IncrementalJsonSource,
    JsonFixtureSource,
    PartsPipeline,
    PartsStore,
    RecordParseError,
)


def make_rows(n: int, *, bad_index: int | None = None, bad_field: str = "price") -> list[dict]:
    rows = [
        {
            "supplier": "Northstar",
            "part_number": f"PN-{i:05d}",
            "brand": "Acme Inc.",
            "description": f"bearing {i}",
            "package_quantity": 1,
            "price": "10.00",
            "stock_status": "In Stock",
        }
        for i in range(n)
    ]
    if bad_index is not None:
        rows[bad_index][bad_field] = "not-a-price"
    return rows


class PipelineBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_jsonl(self, rows: list[dict]) -> Path:
        path = self.dir / "feed.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def test_batches_and_checkpoint_advance_together(self) -> None:
        path = self._write_jsonl(make_rows(10))
        cp_store = CheckpointStore(self.dir / "cp.json")
        store = PartsStore(self.dir / "parts.db")
        stats = PartsPipeline(
            IncrementalJsonSource(path), store, batch_size=3, checkpoint_store=cp_store
        ).run_streaming(source_ref=str(path))
        store.close()

        self.assertEqual(stats.records_processed, 10)
        self.assertEqual(stats.total_records, 10)
        # ceil(10/3) = 4 批
        self.assertEqual(stats.batches_committed, 4)
        self.assertEqual(stats.total_batches, 4)
        cp = cp_store.load()
        self.assertEqual(cp.records_processed, 10)
        self.assertEqual(cp.batch_count, 4)
        # 检查点停在最后一条记录之后；文件末尾换行不属于任何记录
        self.assertEqual(cp.next_offset, path.stat().st_size - 1)
        self.assertEqual(cp.file_format, "jsonl")

    def test_checkpoint_file_is_valid_json_and_atomically_named(self) -> None:
        path = self._write_jsonl(make_rows(2))
        cp_store = CheckpointStore(self.dir / "nested" / "cp.json")
        store = PartsStore(self.dir / "parts.db")
        PartsPipeline(
            IncrementalJsonSource(path), store, batch_size=2, checkpoint_store=cp_store
        ).run_streaming()
        store.close()
        payload = json.loads(cp_store.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["records_processed"], 2)
        self.assertIn("updated_at", payload)
        # 没有遗留临时文件
        self.assertEqual(list(cp_store.path.parent.glob("*.tmp")), [])

    def test_corrupt_checkpoint_raises_checkpoint_error(self) -> None:
        cp_store = CheckpointStore(self.dir / "cp.json")
        cp_store.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            cp_store.load()

    def test_reset_removes_checkpoint(self) -> None:
        from parts_catalog import Checkpoint

        cp_store = CheckpointStore(self.dir / "cp.json")
        cp_store.save(Checkpoint.initial(str((self.dir / "x").resolve()), "jsonl", 0))
        self.assertTrue(cp_store.path.exists())
        cp_store.reset()
        self.assertFalse(cp_store.path.exists())
        cp_store.reset()  # 幂等，不报错


class BadRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bad_record_aborts_with_location_and_safe_offset(self) -> None:
        rows = make_rows(10, bad_index=5)
        path = self.dir / "feed.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        cp_store = CheckpointStore(self.dir / "cp.json")
        store = PartsStore(self.dir / "parts.db")
        pipeline = PartsPipeline(
            IncrementalJsonSource(path), store, batch_size=3, checkpoint_store=cp_store
        )
        with self.assertRaises(RecordParseError) as ctx:
            pipeline.run_streaming(source_ref=str(path))
        exc = ctx.exception
        self.assertEqual(exc.record_sequence, 5)
        self.assertEqual(exc.line, 6)
        # 安全位置 = 最后一个完整批次（3 条）之后；在途的 2 条未提交
        self.assertEqual(exc.safe_records_processed, 3)
        store.close()

        cp = cp_store.load()
        self.assertEqual(cp.records_processed, 3)
        self.assertEqual(cp.batch_count, 1)
        check_store = PartsStore(self.dir / "parts.db")
        self.assertEqual(check_store.count_parts(), 3)
        check_store.close()

    def test_repair_then_resume_processes_remainder_exactly_once(self) -> None:
        rows = make_rows(10, bad_index=5)
        path = self.dir / "feed.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        db_path = self.dir / "parts.db"
        cp_store = CheckpointStore(self.dir / "cp.json")

        store = PartsStore(db_path)
        with self.assertRaises(RecordParseError):
            PartsPipeline(
                IncrementalJsonSource(path), store, batch_size=3, checkpoint_store=cp_store
            ).run_streaming(source_ref=str(path))
        store.close()

        # 等长字节修复，保持所有偏移稳定
        old = b'"price": "not-a-price"'
        new = b'"price": "12.50"' + b" " * (len(old) - len(b'"price": "12.50"'))
        self.assertEqual(len(old), len(new))
        data = path.read_bytes()
        self.assertEqual(data.count(old), 1)
        path.write_bytes(data.replace(old, new))

        store = PartsStore(db_path)
        resume_offset = cp_store.load().next_offset
        stats = PartsPipeline(
            IncrementalJsonSource(path, checkpoint=cp_store.load()),
            store,
            batch_size=3,
            checkpoint_store=cp_store,
        ).run_streaming(source_ref=str(path))
        self.assertEqual(stats.records_processed, 7)
        self.assertEqual(stats.total_records, 10)
        self.assertEqual(stats.resumed_offset, resume_offset)
        self.assertEqual(store.count_parts(), 10)
        store.close()

        # 再跑一次：零新增、零新批次（最终批次计数保持 4）
        store = PartsStore(db_path)
        rerun = PartsPipeline(
            IncrementalJsonSource(path, checkpoint=cp_store.load()),
            store,
            batch_size=3,
            checkpoint_store=cp_store,
        ).run_streaming()
        self.assertEqual(rerun.records_processed, 0)
        self.assertEqual(rerun.total_records, 10)
        self.assertEqual(rerun.total_batches, 4)
        store.close()

    def test_array_format_recovery_end_to_end(self) -> None:
        rows = make_rows(7, bad_index=4)
        path = self.dir / "feed.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        cp_store = CheckpointStore(self.dir / "cp.json")
        store = PartsStore(self.dir / "parts.db")
        with self.assertRaises(RecordParseError):
            PartsPipeline(
                IncrementalJsonSource(path), store, batch_size=2, checkpoint_store=cp_store
            ).run_streaming()
        store.close()
        self.assertEqual(cp_store.load().file_format, "array")
        self.assertEqual(cp_store.load().records_processed, 4)

        old = b'"price": "not-a-price"'
        new = b'"price": "12.50"' + b" " * (len(old) - len(b'"price": "12.50"'))
        path.write_bytes(path.read_bytes().replace(old, new))

        store = PartsStore(self.dir / "parts.db")
        stats = PartsPipeline(
            IncrementalJsonSource(path, checkpoint=cp_store.load()),
            store,
            batch_size=2,
            checkpoint_store=cp_store,
        ).run_streaming()
        self.assertEqual(stats.total_records, 7)
        self.assertEqual(store.count_parts(), 7)
        store.close()


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_legacy_construction_and_run_signature(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "supplier_payload.json"
        store = PartsStore(self.dir / "legacy.db")
        result = PartsPipeline(JsonFixtureSource(fixture), store).run("USD", "fixture")
        self.assertIsInstance(result, int)
        self.assertEqual(result, 2)
        self.assertGreaterEqual(store.count_parts(), 1)
        store.close()

    def test_empty_feed_leaves_initial_checkpoint_and_zero_rows(self) -> None:
        path = self.dir / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        cp_store = CheckpointStore(self.dir / "cp.json")
        store = PartsStore(self.dir / "empty.db")
        stats = PartsPipeline(
            IncrementalJsonSource(path), store, batch_size=10, checkpoint_store=cp_store
        ).run_streaming()
        self.assertEqual(stats.records_processed, 0)
        self.assertEqual(stats.batches_committed, 0)
        self.assertEqual(store.count_parts(), 0)
        self.assertIsNotNone(cp_store.load())
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
