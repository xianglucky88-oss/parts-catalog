"""可恢复摄取检查点。

检查点只描述「已经安全落库到哪一个字节偏移」，与具体来源文件绑定。
写入采用临时文件 + 原子替换，避免进程崩溃留下半个 JSON 文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class CheckpointError(RuntimeError):
    """检查点文件缺失、损坏或与当前来源不匹配。"""


@dataclass(frozen=True)
class Checkpoint:
    source_path: str          # 来源文件的绝对路径
    file_format: str          # "array" 或 "jsonl"
    next_offset: int          # 下一条待处理记录的起始字节偏移
    records_processed: int    # 截至 next_offset 已安全处理的记录数
    batch_count: int          # 已提交的批次数
    file_size: int            # 写入检查点时的文件大小（诊断用）
    updated_at: str           # UTC ISO-8601 时间戳

    @classmethod
    def initial(cls, source_path: str, file_format: str, file_size: int) -> "Checkpoint":
        return cls(
            source_path=source_path,
            file_format=file_format,
            next_offset=0,
            records_processed=0,
            batch_count=0,
            file_size=file_size,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def advanced(self, next_offset: int, records_processed: int, batch_count: int, file_size: int) -> "Checkpoint":
        return Checkpoint(
            source_path=self.source_path,
            file_format=self.file_format,
            next_offset=next_offset,
            records_processed=records_processed,
            batch_count=batch_count,
            file_size=file_size,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "file_format": self.file_format,
            "next_offset": self.next_offset,
            "records_processed": self.records_processed,
            "batch_count": self.batch_count,
            "file_size": self.file_size,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Checkpoint":
        try:
            checkpoint = cls(
                source_path=str(payload["source_path"]),
                file_format=str(payload["file_format"]),
                next_offset=int(payload["next_offset"]),
                records_processed=int(payload["records_processed"]),
                batch_count=int(payload["batch_count"]),
                file_size=int(payload["file_size"]),
                updated_at=str(payload["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(f"检查点字段非法: {exc}") from exc
        if checkpoint.next_offset < 0 or checkpoint.records_processed < 0 or checkpoint.batch_count < 0:
            raise CheckpointError("检查点字段不能为负数")
        if checkpoint.file_format not in ("array", "jsonl"):
            raise CheckpointError(f"未知的来源格式: {checkpoint.file_format}")
        return checkpoint


class CheckpointStore:
    """检查点文件的加载、原子写入与重置。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> Checkpoint | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CheckpointError(f"检查点文件无法解析: {self.path}") from exc
        if not isinstance(payload, dict):
            raise CheckpointError(f"检查点文件结构非法: {self.path}")
        return Checkpoint.from_dict(payload)

    def save(self, checkpoint: Checkpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(checkpoint.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def reset(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
