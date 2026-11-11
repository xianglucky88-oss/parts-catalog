"""CLI 端到端测试：参数兼容、检查点恢复、坏记录退出码与 --reset-checkpoint。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parts_catalog.cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_feed(self, rows: list[dict], name: str = "feed.jsonl") -> Path:
        path = self.dir / name
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def _rows(self, n: int) -> list[dict]:
        return [
            {
                "supplier": "S",
                "part_number": f"P-{i}",
                "brand": "ACME",
                "package_quantity": 1,
                "price": "10.00",
            }
            for i in range(n)
        ]

    def test_success_returns_zero_and_writes_checkpoint(self) -> None:
        feed = self._write_feed(self._rows(5))
        db = self.dir / "parts.db"
        code = main([
            "--fixture", str(feed), "--database", str(db),
            "--batch-size", "2",
        ])
        self.assertEqual(code, 0)
        cp = json.loads((self.dir / "parts.db.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(cp["records_processed"], 5)
        self.assertEqual(cp["batch_count"], 3)  # ceil(5/2)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 5)

    def test_bad_record_returns_one_with_safe_offset(self) -> None:
        rows = self._rows(4)
        rows[2]["price"] = "broken"
        feed = self._write_feed(rows)
        db = self.dir / "parts.db"
        code = main([
            "--fixture", str(feed), "--database", str(db),
            "--batch-size", "2",
        ])
        self.assertEqual(code, 1)
        cp = json.loads((self.dir / "parts.db.checkpoint.json").read_text(encoding="utf-8"))
        # 只提交了一个完整批次（2 条），坏记录及其后内容未入库
        self.assertEqual(cp["records_processed"], 2)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 2)

    def test_resume_after_repair_processes_remainder(self) -> None:
        rows = self._rows(4)
        rows[2]["price"] = "broken"
        feed = self._write_feed(rows)
        db = self.dir / "parts.db"
        self.assertEqual(main(["--fixture", str(feed), "--database", str(db), "--batch-size", "2"]), 1)

        old = b'"price": "broken"'
        new = b'"price": "12.00"' + b" " * (len(old) - len(b'"price": "12.00"'))
        feed.write_bytes(feed.read_bytes().replace(old, new))

        code = main(["--fixture", str(feed), "--database", str(db), "--batch-size", "2"])
        self.assertEqual(code, 0)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 4)

    def test_reset_checkpoint_reingests_from_start(self) -> None:
        import io
        import contextlib

        feed = self._write_feed(self._rows(3))
        db = self.dir / "parts.db"
        cp_path = self.dir / "parts.db.checkpoint.json"
        self.assertEqual(main(["--fixture", str(feed), "--database", str(db)]), 0)
        self.assertTrue(cp_path.exists())

        # 不重置：检查点已在末尾，本次零新增
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--fixture", str(feed), "--database", str(db)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue().strip().splitlines()[-1])["processed"], 0)

        # 重置：删除检查点后从偏移 0 重新处理全部 3 条
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--fixture", str(feed), "--database", str(db), "--reset-checkpoint"])
        self.assertEqual(code, 0)
        summary = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(summary["processed"], 3)
        self.assertEqual(summary["resumed_from_offset"], 0)
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
        self.assertEqual(cp["records_processed"], 3)

    def test_custom_checkpoint_path_and_array_fixture(self) -> None:
        feed = self.dir / "array.json"
        feed.write_text(json.dumps(self._rows(3)), encoding="utf-8")
        cp = self.dir / "nested" / "cp.json"
        code = main([
            "--fixture", str(feed), "--database", str(self.dir / "a.db"),
            "--checkpoint", str(cp),
        ])
        self.assertEqual(code, 0)
        payload = json.loads(cp.read_text(encoding="utf-8"))
        self.assertEqual(payload["file_format"], "array")

    def test_corrupt_checkpoint_returns_two(self) -> None:
        feed = self._write_feed(self._rows(2))
        cp = self.dir / "parts.db.checkpoint.json"
        cp.write_text("{损坏", encoding="utf-8")
        code = main(["--fixture", str(feed), "--database", str(self.dir / "parts.db")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
