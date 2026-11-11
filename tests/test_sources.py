"""来源层增量扫描、双格式、错误定位与检查点恢复测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parts_catalog import (  # noqa: E402
    Checkpoint,
    CheckpointMismatchError,
    IncrementalJsonSource,
    JsonFixtureSource,
    RecordParseError,
    SourceFormatError,
    detect_format,
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class FormatDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_detect_array_and_jsonl_including_leading_whitespace(self) -> None:
        array = write(self.dir / "a.json", "  \n\t [{}]")
        jsonl = write(self.dir / "b.jsonl", '\n  {"id": 1}\n')
        self.assertEqual(detect_format(array), "array")
        self.assertEqual(detect_format(jsonl), "jsonl")

    def test_detect_rejects_garbage_and_defaults_empty_file(self) -> None:
        garbage = write(self.dir / "g", ":::")
        empty = write(self.dir / "e", "")
        with self.assertRaises(SourceFormatError):
            detect_format(garbage)
        # 空白文件按 JSONL 零记录来源处理
        self.assertEqual(detect_format(empty), "jsonl")
        self.assertEqual(list(IncrementalJsonSource(empty).iter_records()), [])


class ArrayFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_existing_fixture_streams_records_with_offsets(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "supplier_payload.json"
        source = IncrementalJsonSource(fixture)
        records = list(source.iter_records())
        self.assertEqual([r.sequence for r in records], [0, 1])
        # 数组模式下首条记录起点位于开括号 '[' 与换行之后
        self.assertEqual(records[0].start_offset, 4)
        # 记录之间隔着 ", " 等分隔符，偏移单调推进、区间互不重叠
        self.assertLess(records[0].end_offset, records[1].start_offset)
        self.assertTrue(all(r.end_offset > r.start_offset for r in records))
        self.assertTrue(all(isinstance(r.data, dict) for r in records))
        # 旧接口
        self.assertEqual(len(JsonFixtureSource(fixture).read()), 2)

    def test_pretty_printed_multiline_array(self) -> None:
        path = write(self.dir / "pretty.json", '[\n  {"id": 1},\n  {"id": 2}\n]\n')
        records = list(IncrementalJsonSource(path).iter_records())
        self.assertEqual([(r.sequence, r.line) for r in records], [(0, 2), (1, 3)])
        self.assertEqual([r.data["id"] for r in records], [1, 2])

    def test_compact_array_with_nesting_escapes_and_unicode(self) -> None:
        payload = [
            {"a": {"b": [1, 2, {"c": '},"x"'}]}},
            {"name": "轴承 —20mm", "ok": True, "n": None},
            {"f": -3.5e2},
        ]
        path = write(self.dir / "nested.json", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        records = list(IncrementalJsonSource(path).iter_records())
        self.assertEqual(records[0].data["a"]["b"][2]["c"], '},"x"')
        self.assertEqual(records[1].data["name"], "轴承 —20mm")
        self.assertIsNone(records[1].data["n"])
        self.assertEqual(records[2].data["f"], -350.0)

    def test_array_with_scalar_element_is_rejected_not_skipped(self) -> None:
        path = write(self.dir / "scalars.json", '[{"id": 1}, 42, {"id": 3}]')
        iterator = IncrementalJsonSource(path).iter_records()
        self.assertEqual(next(iterator).data["id"], 1)
        with self.assertRaises(RecordParseError) as ctx:
            next(iterator)
        self.assertEqual(ctx.exception.record_sequence, 1)

    def test_trailing_content_after_array_is_format_error(self) -> None:
        path = write(self.dir / "tail.json", '[{"id": 1}] garbage')
        with self.assertRaises(SourceFormatError):
            list(IncrementalJsonSource(path).iter_records())

    def test_truncated_array_raises_with_offset(self) -> None:
        path = write(self.dir / "trunc.json", '[{"id": 1}, {"id": 2}')
        with self.assertRaises(RecordParseError) as ctx:
            list(IncrementalJsonSource(path).iter_records())
        self.assertIsNotNone(ctx.exception.offset)


class JsonLinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_jsonl_with_blank_lines_and_multiline_object(self) -> None:
        path = write(
            self.dir / "feed.jsonl",
            '{"id": 1}\n\n{\n  "id": 2\n}\n{"id": 3}\n',
        )
        records = list(IncrementalJsonSource(path).iter_records())
        self.assertEqual([r.data["id"] for r in records], [1, 2, 3])
        self.assertEqual(records[1].line, 3)
        # 第一条记录结束后隔着一个空行才是第二条，扫描器跳过空白
        self.assertLess(records[0].end_offset, records[1].start_offset)

    def test_bad_record_in_the_middle_reports_sequence_line_and_offset(self) -> None:
        path = write(
            self.dir / "bad.jsonl",
            '{"id": 1}\n{"id": 2 "x": 3}\n{"id": 3}\n',
        )
        with self.assertRaises(RecordParseError) as ctx:
            list(IncrementalJsonSource(path).iter_records())
        exc = ctx.exception
        self.assertEqual(exc.record_sequence, 1)
        self.assertEqual(exc.line, 2)
        self.assertIsNotNone(exc.offset)
        # 坏记录之前的记录仍然可被独立读取（扫描停在坏记录处而非静默跳过）
        good = []
        iterator = IncrementalJsonSource(path).iter_records()
        good.append(next(iterator))
        with self.assertRaises(RecordParseError):
            next(iterator)
        self.assertEqual(good[0].data["id"], 1)

    def test_unclosed_record_is_not_silently_skipped(self) -> None:
        path = write(self.dir / "unclosed.jsonl", '{"id": 1}\n{"id": 2')
        with self.assertRaises(RecordParseError):
            list(IncrementalJsonSource(path).iter_records())

    def test_invalid_utf8_is_reported_as_parse_error(self) -> None:
        path = self.dir / "raw.jsonl"
        path.write_bytes(b'{"id": 1}\n{"id": "\xff\xfe"}\n')
        with self.assertRaises(RecordParseError) as ctx:
            list(IncrementalJsonSource(path).iter_records())
        self.assertEqual(ctx.exception.record_sequence, 1)


class ChunkBoundaryTests(unittest.TestCase):
    """强制极小分块，保证跨块逻辑与块大小无关。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_results_for_various_chunk_sizes(self) -> None:
        rows = [{"id": i, "name": "件" * (i + 1), "meta": {"x": [1, 2, 3]}} for i in range(50)]
        jsonl = write(self.dir / "f.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        array = write(self.dir / "f.json", json.dumps(rows, ensure_ascii=False))
        baseline = [r.data for r in IncrementalJsonSource(jsonl, chunk_size=1 << 20).iter_records()]
        for size in (4096, 4097, 8192, 100000):
            streamed = [r.data for r in IncrementalJsonSource(jsonl, chunk_size=size).iter_records()]
            self.assertEqual(streamed, baseline)
            array_records = [r.data for r in IncrementalJsonSource(array, chunk_size=size).iter_records()]
            self.assertEqual(array_records, rows)
            # 所有分块大小下记录边界一致
            offsets_a = [(r.start_offset, r.end_offset) for r in
                         IncrementalJsonSource(jsonl, chunk_size=size).iter_records()]
            offsets_b = [(r.start_offset, r.end_offset) for r in
                         IncrementalJsonSource(jsonl, chunk_size=4096).iter_records()]
            self.assertEqual(offsets_a, offsets_b)


class CheckpointResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _feed(self, n: int) -> Path:
        rows = [{"id": i, "part_number": f"P-{i}"} for i in range(n)]
        path = self.dir / "feed.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def test_resume_continues_sequence_and_skips_safe_prefix(self) -> None:
        path = self._feed(6)
        full = list(IncrementalJsonSource(path).iter_records())
        boundary = full[3].end_offset
        cp = Checkpoint.initial(
            str(path.resolve()), "jsonl", path.stat().st_size
        ).advanced(boundary, 4, 1, path.stat().st_size)
        resumed = list(IncrementalJsonSource(path, checkpoint=cp).iter_records())
        self.assertEqual([r.sequence for r in resumed], [4, 5])
        self.assertEqual([r.data["id"] for r in resumed], [4, 5])

    def test_resume_mid_record_inside_gap_whitespace_still_works(self) -> None:
        path = write(self.dir / "g.jsonl", '{"id":1}\n\n\n{"id":2}\n')
        full = list(IncrementalJsonSource(path).iter_records())
        # 偏移落在记录之间的空白区域（而非精确边界）也应恢复到下一条
        gap = full[0].end_offset + 1
        cp = Checkpoint.initial(
            str(path.resolve()), "jsonl", path.stat().st_size
        ).advanced(gap, 1, 1, path.stat().st_size)
        resumed = list(IncrementalJsonSource(path, checkpoint=cp).iter_records())
        self.assertEqual([r.sequence for r in resumed], [1])

    def test_checkpoint_rejects_wrong_path_format_and_oversized_offset(self) -> None:
        path = self._feed(3)
        other = write(self.dir / "other.jsonl", '{"id": 9}\n')
        cp_wrong_path = Checkpoint.initial(str(other.resolve()), "jsonl", other.stat().st_size)
        with self.assertRaises(CheckpointMismatchError):
            list(IncrementalJsonSource(path, checkpoint=cp_wrong_path).iter_records())

        cp_wrong_fmt = Checkpoint.initial(str(path.resolve()), "array", path.stat().st_size)
        with self.assertRaises(CheckpointMismatchError):
            list(IncrementalJsonSource(path, checkpoint=cp_wrong_fmt).iter_records())

        cp_big = Checkpoint.initial(
            str(path.resolve()), "jsonl", path.stat().st_size
        ).advanced(path.stat().st_size + 10, 1, 1, path.stat().st_size)
        with self.assertRaises(CheckpointMismatchError):
            list(IncrementalJsonSource(path, checkpoint=cp_big).iter_records())

    def test_offsets_are_byte_exact_under_multibyte_content(self) -> None:
        path = write(
            self.dir / "u.jsonl",
            '{"name": "轴承"}\n{"name": "密封件"}\n',
        )
        records = list(IncrementalJsonSource(path).iter_records())
        boundary = records[0].end_offset
        cp = Checkpoint.initial(
            str(path.resolve()), "jsonl", path.stat().st_size
        ).advanced(boundary, 1, 1, path.stat().st_size)
        resumed = list(IncrementalJsonSource(path, checkpoint=cp).iter_records())
        self.assertEqual(resumed[0].data["name"], "密封件")
        self.assertEqual(resumed[0].sequence, 1)

    def test_utf8_bom_is_skipped_in_both_formats(self) -> None:
        jsonl = self.dir / "bom.jsonl"
        jsonl.write_bytes(b"\xef\xbb\xbf" + '{"id": 1}\n{"id": 2}\n'.encode("utf-8"))
        records = list(IncrementalJsonSource(jsonl).iter_records())
        self.assertEqual([r.data["id"] for r in records], [1, 2])
        self.assertEqual(records[0].start_offset, 3)  # BOM 之后才是记录起点
        self.assertEqual(detect_format(jsonl), "jsonl")

        array = self.dir / "bom.json"
        array.write_bytes(b"\xef\xbb\xbf" + b'[{"id": 1}]')
        array_records = list(IncrementalJsonSource(array).iter_records())
        self.assertEqual(array_records[0].data["id"], 1)
        self.assertEqual(detect_format(array), "array")


@unittest.skipUnless(os.environ.get("RUN_SLOW_TESTS") == "1", "设置 RUN_SLOW_TESTS=1 运行大文件内存测试")
class LargeFileMemoryTests(unittest.TestCase):
    def test_peak_memory_is_bounded_by_chunk_size_not_file_size(self) -> None:
        import resource

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "large.jsonl"
            target_bytes = 80 * 1024 * 1024
            written = 0
            seq = 0
            with path.open("w", encoding="utf-8") as handle:
                while written < target_bytes:
                    row = {
                        "supplier": "Northstar",
                        "part_number": f"PN-{seq:08d}",
                        "brand": "ACME",
                        "description": "密封轴承 " * 8,
                        "package_quantity": 1,
                        "price": "18.50",
                    }
                    line = json.dumps(row, ensure_ascii=False) + "\n"
                    handle.write(line)
                    written += len(line.encode("utf-8"))
                    seq += 1
            total_records = seq
            file_size = path.stat().st_size

            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            source = IncrementalJsonSource(path, chunk_size=64 * 1024)
            count = 0
            for record in source.iter_records():
                count += 1
                self.assertEqual(record.sequence, count - 1)
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            self.assertEqual(count, total_records)
            # 80MB 文件 + 64KB 分块：峰值增量应远小于文件大小（KB 单位）
            self.assertLess(after - before, 8 * 1024)  # 增量 < 8MB
            self.assertGreater(file_size, 80 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
