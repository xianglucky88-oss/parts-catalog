"""来源层：增量、可恢复的目录摄取。

设计目标：
* 不把整个供应商文件装入内存——按固定大小的字节块流式扫描，峰值内存
  只与 ``chunk_size`` 和单条记录大小有关，与文件总大小无关；
* 同时兼容现有的 JSON 数组 fixture 与逐行 JSON（JSON Lines / NDJSON，
  也允许单条对象跨多行书写）；
* 每条记录都携带全局序号与精确的字节偏移，处理位置可记录、可恢复；
* 坏记录一律抛出 :class:`IngestionError` 并指明位置，绝不静默跳过。

外部调用保持兼容：旧代码中的 ``source.read()`` 仍然返回完整列表，
新代码应使用 ``iter_records()`` 逐条拉取。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Protocol

from .checkpoint import Checkpoint

# 与 JSON 词法相关的字节常量，全部落在 ASCII 范围内，可直接在 UTF-8
# 字节流上做结构判断（多字节序列的续字节最高位为 1，不会与这些值冲突）。
_B_QUOTE = ord('"')
_B_BACKSLASH = ord("\\")
_B_OPEN_ARRAY = ord("[")
_B_CLOSE_ARRAY = ord("]")
_B_OPEN_OBJECT = ord("{")
_B_CLOSE_OBJECT = ord("}")
_B_COMMA = ord(",")
_B_COLON = ord(":")
_B_NEWLINE = ord("\n")
_WHITESPACE = frozenset((0x20, 0x09, 0x0A, 0x0D))
_SCALAR_START = frozenset((ord("t"), ord("f"), ord("n"), ord("-"))) | set(
    b"0123456789"
)
# UTF-8 BOM，只允许出现在文件最开头（Windows 供应商导出常见）。
_BOM = b"\xef\xbb\xbf"


class IngestionError(RuntimeError):
    """摄取过程中的所有错误的共同基类，携带处理位置。"""

    def __init__(
        self,
        message: str,
        *,
        path: str | Path | None = None,
        record_sequence: Optional[int] = None,
        offset: Optional[int] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.path = str(path) if path is not None else None
        self.record_sequence = record_sequence
        self.offset = offset
        self.line = line
        self.column = column

    def location(self) -> str:
        bits = []
        if self.path:
            bits.append(f"文件 {self.path}")
        if self.record_sequence is not None:
            bits.append(f"第 {self.record_sequence} 条记录")
        if self.offset is not None:
            bits.append(f"字节偏移 {self.offset}")
        if self.line is not None:
            bits.append(f"第 {self.line} 行")
        if self.column is not None:
            bits.append(f"第 {self.column} 列")
        return "（" + "，".join(bits) + "）" if bits else "位置未知"


class RecordParseError(IngestionError):
    """单条记录无法解析为合法 JSON，或顶层元素不是 JSON 对象。"""


class SourceFormatError(IngestionError):
    """文件整体结构非法（不是数组也不是 JSON Lines）。"""


class CheckpointMismatchError(IngestionError):
    """检查点与当前文件/格式不匹配，或偏移落在非法位置。"""


@dataclass(frozen=True)
class IngestRecord:
    """一次增量读取产出的单条记录。"""

    sequence: int                  # 全局从 0 开始的记录序号（跨恢复保持稳定）
    start_offset: int              # 记录起始字节偏移
    end_offset: int                # 记录结束后的字节偏移（即下一条的起点）
    line: int                      # 记录起始行号（从 1 开始，便于人工定位）
    column: int                    # 记录起始列号（从 1 开始）
    data: dict                     # 解析后的 JSON 对象


def _offset_to_line(path: Path, offset: int) -> tuple[int, int]:
    """把字节偏移换算成 1 起始的行号、列号；仅在报错路径上调用。"""
    line = 1
    last_newline = -1
    remaining = offset
    with path.open("rb") as handle:
        while remaining > 0:
            block = handle.read(min(remaining, 1 << 20))
            if not block:
                break
            for idx, byte in enumerate(block):
                if byte == _B_NEWLINE:
                    line += 1
                    last_newline = handle.tell() - len(block) + idx
            remaining -= len(block)
    column = offset - last_newline
    return line, column


def _count_prefix_newlines(path: Path, offset: int) -> int:
    """恢复摄取时，用一次顺序扫描确定起始偏移之前的行数。"""
    if offset <= 0:
        return 0
    total = 0
    remaining = offset
    with path.open("rb") as handle:
        while remaining > 0:
            block = handle.read(min(remaining, 1 << 20))
            if not block:
                break
            total += block.count(b"\n")
            remaining -= len(block)
    return total


def _scan_top_level_values(
    path: Path, file_format: str, start_offset: int = 0, chunk_size: int = 1 << 20
) -> Iterator[tuple[bytes, int, int, int, int]]:
    """流式切分顶层 JSON 值。

    产出 ``(raw_bytes, start_offset, end_offset, line, column)``。
    扫描器只做词法层面的括号配对与字符串状态判断，不解析具体值，
    因此内存占用与文件总大小无关。
    """
    base_depth = 1 if file_format == "array" else 0

    buffer = b""                 # buffer[0] 对应绝对偏移 anchor
    anchor = start_offset
    index = 0
    depth = base_depth if start_offset > 0 else 0
    array_seen = start_offset > 0 and file_format == "array"
    in_string = False
    escaped = False
    in_container = False         # 当前记录是对象/数组容器
    in_scalar = False            # 当前记录是顶层标量
    rec_start: Optional[int] = None
    line = 1 + _count_prefix_newlines(path, start_offset)
    column = 1
    rec_line = line
    rec_column = column

    with path.open("rb") as handle:
        if start_offset == 0 and handle.read(3) == _BOM:
            # BOM 不属于任何记录：直接把它排除在可寻址内容之外。
            handle.seek(3)
            anchor = 3
        else:
            handle.seek(start_offset)
        while True:
            if index >= len(buffer):
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                buffer += chunk
            byte = buffer[index]
            abs_offset = anchor + index

            if byte == _B_NEWLINE:
                line += 1
                column = 1
            else:
                column += 1

            if in_string:
                if escaped:
                    escaped = False
                elif byte == _B_BACKSLASH:
                    escaped = True
                elif byte == _B_QUOTE:
                    in_string = False
                index += 1
                continue

            if not in_container and not in_scalar:
                # 记录之间的空闲状态
                if byte in _WHITESPACE:
                    index += 1
                    continue
                if file_format == "array":
                    if byte == _B_OPEN_ARRAY and depth == 0 and not array_seen:
                        array_seen = True
                        depth = 1
                        index += 1
                        continue
                    if byte == _B_CLOSE_ARRAY and depth == base_depth:
                        depth -= 1
                        index += 1
                        continue
                    if byte == _B_COMMA and depth == base_depth:
                        index += 1
                        continue
                if depth != base_depth:
                    raise SourceFormatError(
                        f"意外的字节 0x{byte:02x}（'{chr(byte) if byte < 128 else '?'}'），"
                        "JSON 结构已损坏",
                        path=path, offset=abs_offset, line=line, column=column,
                    )
                if byte in (_B_COLON, _B_CLOSE_OBJECT, _B_CLOSE_ARRAY, _B_COMMA):
                    raise SourceFormatError(
                        f"记录之间出现非法字符 '{chr(byte)}'",
                        path=path, offset=abs_offset, line=line, column=column,
                    )
                if byte in (_B_OPEN_OBJECT, _B_OPEN_ARRAY):
                    rec_start = abs_offset
                    rec_line, rec_column = line, column
                    depth += 1
                    in_container = True
                    index += 1
                    continue
                if byte in _SCALAR_START:
                    rec_start = abs_offset
                    rec_line, rec_column = line, column
                    in_scalar = True
                    index += 1
                    continue
                raise SourceFormatError(
                    f"无法识别的字节 0x{byte:02x}，JSON 结构已损坏",
                    path=path, offset=abs_offset, line=line, column=column,
                )

            # 正在读取一条记录
            if byte == _B_QUOTE:
                in_string = True
            elif byte in (_B_OPEN_OBJECT, _B_OPEN_ARRAY):
                depth += 1
            elif byte in (_B_CLOSE_OBJECT, _B_CLOSE_ARRAY):
                depth -= 1
                if in_container and depth == base_depth:
                    index += 1
                    end_offset = anchor + index
                    raw = buffer[rec_start - anchor:end_offset - anchor]
                    yield raw, rec_start, end_offset, rec_line, rec_column
                    # 压缩已消费前缀，缓冲区只保留尾部
                    buffer = buffer[end_offset - anchor:]
                    anchor = end_offset
                    index = 0
                    in_container = False
                    rec_start = None
                    continue
            elif in_scalar and (byte == _B_COMMA or byte in _WHITESPACE):
                # 标量记录结束（分隔符不属于记录本身）
                end_offset = abs_offset
                raw = buffer[rec_start - anchor:end_offset - anchor]
                yield raw, rec_start, end_offset, rec_line, rec_column
                buffer = buffer[end_offset - anchor:]
                anchor = end_offset
                index = 0
                in_scalar = False
                rec_start = None
                continue
            index += 1

    if in_string or in_container or in_scalar:
        where = rec_start if rec_start is not None else anchor
        raise RecordParseError(
            "文件在一条未结束的 JSON 记录处截断",
            path=path, offset=where,
        )
    if file_format == "array" and depth != 0:
        raise RecordParseError(
            "JSON 数组在结束前被截断", path=path, offset=anchor,
        )
    if depth < 0 or (file_format == "array" and not array_seen):
        raise SourceFormatError("文件结构非法，未找到 JSON 数组", path=path)


def detect_format(path: str | Path) -> str:
    """根据首个非空白字节判断来源格式：``array`` 或 ``jsonl``。

    空白文件没有任何结构线索，按 JSON Lines 惯例视为零记录来源；
    显式传入 ``file_format`` 或检查点可覆盖该默认值。
    """
    path = Path(path)
    with path.open("rb") as handle:
        head = handle.read(3)
        if head == _BOM:
            handle.seek(3)  # BOM 不是 JSON 词法的一部分，跳过
        else:
            handle.seek(0)
        while True:
            block = handle.read(4096)
            if not block:
                return "jsonl"
            for byte in block:
                if byte in _WHITESPACE:
                    continue
                if byte == _B_OPEN_ARRAY:
                    return "array"
                if byte == _B_OPEN_OBJECT or byte in _SCALAR_START:
                    return "jsonl"
                raise SourceFormatError(
                    f"文件既不是 JSON 数组也不是逐行 JSON（首个有效字节 0x{byte:02x}）",
                    path=path,
                )


class PartSource(Protocol):
    """来源协议。新实现使用 ``iter_records``；``read`` 仅为兼容保留。"""

    def iter_records(self) -> Iterator[IngestRecord]: ...

    def read(self) -> list[dict]: ...


class IncrementalJsonSource:
    """增量 JSON 来源，支持数组、JSON Lines 与检查点恢复。

    每次只在内存中保留一个分块和当前正在成形的记录，文件可达数百 MB
    而内存占用保持有界。``checkpoint`` 不为 ``None`` 时，从检查点记录的
    字节偏移继续，记录全局序号跨恢复保持稳定。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        checkpoint: Optional[Checkpoint] = None,
        chunk_size: int = 1 << 20,
        file_format: Optional[str] = None,
    ) -> None:
        if chunk_size < 4096:
            raise ValueError("chunk_size 不能小于 4096 字节")
        self.path = Path(path)
        self.chunk_size = chunk_size
        self.file_format = file_format or detect_format(self.path)
        self.file_size = self.path.stat().st_size
        self.start_offset = 0
        self.start_sequence = 0
        self.resumed_from: Optional[Checkpoint] = checkpoint
        if checkpoint is not None:
            self._apply_checkpoint(checkpoint)

    def _apply_checkpoint(self, checkpoint: Checkpoint) -> None:
        resolved = str(self.path.resolve())
        if checkpoint.source_path != resolved:
            raise CheckpointMismatchError(
                f"检查点属于 {checkpoint.source_path}，与当前来源 {resolved} 不匹配",
                path=self.path,
            )
        if checkpoint.file_format != self.file_format:
            raise CheckpointMismatchError(
                f"检查点记录格式为 {checkpoint.file_format}，当前文件为 {self.file_format}",
                path=self.path,
            )
        if checkpoint.next_offset > self.file_size:
            raise CheckpointMismatchError(
                f"检查点偏移 {checkpoint.next_offset} 超出文件大小 {self.file_size}",
                path=self.path, offset=checkpoint.next_offset,
            )
        self.start_offset = checkpoint.next_offset
        self.start_sequence = checkpoint.records_processed

    def iter_records(self) -> Iterator[IngestRecord]:
        sequence = self.start_sequence
        for raw, start, end, line, column in _scan_top_level_values(
            self.path, self.file_format, self.start_offset, self.chunk_size
        ):
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                line_no, col_no = _offset_to_line(self.path, start)
                preview = raw[:120].decode("utf-8", errors="replace")
                raise RecordParseError(
                    f"第 {sequence} 条记录不是合法 JSON：{exc}；片段预览: {preview!r}",
                    path=self.path, record_sequence=sequence,
                    offset=start, line=line_no, column=col_no,
                ) from exc
            if not isinstance(data, dict):
                line_no, col_no = _offset_to_line(self.path, start)
                raise RecordParseError(
                    f"第 {sequence} 条顶层元素是 {type(data).__name__}，记录必须是 JSON 对象",
                    path=self.path, record_sequence=sequence,
                    offset=start, line=line_no, column=col_no,
                )
            yield IngestRecord(
                sequence=sequence,
                start_offset=start,
                end_offset=end,
                line=line,
                column=column,
                data=data,
            )
            sequence += 1

    def read(self) -> list[dict]:
        """兼容旧接口：物化全部记录。大文件请改用 ``iter_records``。"""
        return [record.data for record in self.iter_records()]


class JsonFixtureSource:
    """旧接口的兼容包装，内部已切换为增量扫描。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._impl = IncrementalJsonSource(self.path)

    def iter_records(self) -> Iterator[IngestRecord]:
        return self._impl.iter_records()

    def read(self) -> list[dict]:
        return self._impl.read()
