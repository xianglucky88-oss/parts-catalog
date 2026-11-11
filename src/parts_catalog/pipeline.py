"""摄取管道：分批处理、按批提交检查点、出错时定位安全恢复起点。

容错原则：坏记录不会被悄悄跳过——任何解析或规范化失败都会抛出
:class:`~parts_catalog.sources.IngestionError`，并附带「已经安全处理到
哪里」的检查点信息，调用方修复来源后可从该偏移原样恢复。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .checkpoint import Checkpoint, CheckpointStore
from .normalize import normalize_part, to_catalog_part, to_quote
from .sources import IngestRecord, IngestionError, IncrementalJsonSource, PartSource
from .storage import PartsStore


@dataclass(frozen=True)
class RunStats:
    """一次管道运行的处理位置与批次计数。"""

    records_processed: int       # 本次运行实际处理的记录数
    total_records: int           # 含恢复前历史在内的累计记录数
    batches_committed: int       # 本次运行提交的批次数
    total_batches: int           # 含恢复前历史在内的累计批次数
    last_offset: int             # 最后一条已提交记录之后的字节偏移
    resumed_offset: int          # 本次运行的起始字节偏移
    checkpoint_path: Optional[str] = None


class PartsPipeline:
    def __init__(
        self,
        source: PartSource,
        store: PartsStore,
        *,
        batch_size: int = 500,
        checkpoint_store: Optional[CheckpointStore] = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size 必须 >= 1")
        self.source = source
        self.store = store
        self.batch_size = batch_size
        self.checkpoint_store = checkpoint_store

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> int:
        """兼容旧签名：返回本次运行处理的记录条数。"""
        return self.run_streaming(currency, source_ref).records_processed

    # ------------------------------------------------------------------
    def run_streaming(self, currency: str = "USD", source_ref: str = "fixture") -> RunStats:
        if not isinstance(self.source, IncrementalJsonSource):
            # 兼容只实现 read() 的第三方来源：一次性物化后按批处理。
            return self._run_legacy(currency, source_ref)

        source = self.source
        resumed: Optional[Checkpoint] = source.resumed_from
        safe_offset = source.start_offset
        base_records = source.start_sequence
        base_batches = resumed.batch_count if resumed is not None else 0
        processed_this_run = 0
        committed_this_run = 0
        batches_this_run = 0
        pending = 0
        last_offset = source.start_offset

        checkpoint: Optional[Checkpoint] = resumed or (
            Checkpoint.initial(
                source_path=str(source.path.resolve()),
                file_format=source.file_format,
                file_size=source.file_size,
            )
        )

        def commit_batch(end_offset: int) -> None:
            nonlocal checkpoint, batches_this_run, pending, committed_this_run
            self.store.commit()
            checkpoint = checkpoint.advanced(
                next_offset=end_offset,
                records_processed=base_records + committed_this_run + pending,
                batch_count=base_batches + batches_this_run + 1,
                file_size=source.file_size,
            )
            if self.checkpoint_store is not None:
                self.checkpoint_store.save(checkpoint)
            batches_this_run += 1
            committed_this_run += pending
            pending = 0

        try:
            for record in source.iter_records():
                self._process_one(record, currency, source_ref)
                processed_this_run += 1
                pending += 1
                last_offset = record.end_offset
                if pending >= self.batch_size:
                    commit_batch(record.end_offset)
                    safe_offset = record.end_offset
            if pending:
                commit_batch(last_offset)
                safe_offset = last_offset
            elif self.checkpoint_store is not None and resumed is None:
                # 空文件也要留下初始检查点，让运行结果可观测。
                self.checkpoint_store.save(checkpoint)
        except IngestionError as exc:
            # 回滚尚未成批的在途写入，使 SQLite 的已持久化状态与检查点
            # 严格对齐到最后一个完整批次；坏记录不会留下半批脏数据。
            self.store.rollback()
            # 检查点与 DB 都停留在最后一个完整批次；safe_offset 即恢复起点。
            # 在途记录（pending）尚未提交，不计入「已安全处理」数量。
            if exc.offset is None:
                exc.offset = safe_offset
            exc.safe_offset = safe_offset  # type: ignore[attr-defined]
            exc.safe_records_processed = base_records + committed_this_run  # type: ignore[attr-defined]
            raise

        return RunStats(
            records_processed=processed_this_run,
            total_records=base_records + processed_this_run,
            batches_committed=batches_this_run,
            total_batches=base_batches + batches_this_run,
            last_offset=safe_offset,
            resumed_offset=source.start_offset,
            checkpoint_path=str(self.checkpoint_store.path) if self.checkpoint_store else None,
        )

    def _process_one(
        self,
        record: IngestRecord,
        currency: str,
        source_ref: str,
    ) -> None:
        try:
            supplier_part = normalize_part(record.data)
        except (ValueError, KeyError, TypeError) as exc:
            from .sources import RecordParseError

            error = RecordParseError(
                f"记录规范化失败：{exc}",
                path=self.source.path if isinstance(self.source, IncrementalJsonSource) else None,
                record_sequence=record.sequence,
                offset=record.start_offset,
                line=record.line,
                column=record.column,
            )
            raise error from exc
        self.store.save_part(to_catalog_part(supplier_part))
        self.store.save_quote(to_quote(supplier_part, currency, source_ref))

    def _run_legacy(self, currency: str, source_ref: str) -> RunStats:
        count = 0
        for raw in self.source.read():
            supplier_part = normalize_part(raw)
            self.store.save_part(to_catalog_part(supplier_part))
            self.store.save_quote(to_quote(supplier_part, currency, source_ref))
            count += 1
        self.store.commit()
        return RunStats(
            records_processed=count,
            total_records=count,
            batches_committed=1 if count else 0,
            total_batches=1 if count else 0,
            last_offset=0,
            resumed_offset=0,
        )
