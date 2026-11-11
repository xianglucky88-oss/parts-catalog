# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 来源层：增量摄取与可恢复检查点

供应商现在可能交付数百 MB 的全量目录。来源层不再把整个文件读入内存，而是按
固定字节块（默认 1 MiB）流式扫描，内存峰值只与分块大小和单条记录有关，与
文件总大小无关。

* **两种格式同一套机制**：兼容现有 JSON 数组 fixture（`[{...}, {...}]`）与
  逐行 JSON / JSON Lines（NDJSON，单条对象也允许跨多行书写），按文件首个
  有效字节自动识别，也可显式指定。
* **每条记录携带处理位置**：全局序号、起始/结束字节偏移、行列号。
* **批次计数与检查点**：每处理满一个批次（默认 500 条）同时提交 SQLite 事务
  并原子写入检查点，记录 `next_offset`、累计记录数、批次数与来源标识。
* **坏记录不跳过**：解析或规范化失败立即抛出带序号/行列/字节偏移的
  `IngestionError`，并在错误上给出**安全恢复起点**（最后一个完整批次的偏移
  与已安全处理条数）；在途的半批数据会回滚，数据库磁盘状态与检查点严格一致。
* **可恢复**：修复来源文件后重跑，自动从检查点偏移继续，剩余记录恰好处理
  一次；检查点与文件路径/格式不匹配时显式报错而不是静默重摄取。

### 检查点文件

默认放在数据库旁（`<database>.checkpoint.json`），采用临时文件 + 原子替换：

```json
{
  "source_path": "/abs/path/to/feed.jsonl",
  "file_format": "jsonl",
  "next_offset": 16522746,
  "records_processed": 50000,
  "batch_count": 25,
  "file_size": 125829120,
  "updated_at": "2026-11-11T14:40:00+00:00"
}
```

## 运行 fixture

命令行用法与旧版保持兼容，输出改为可被 `jq` 消费的 JSON：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli \
  --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--batch-size N` | 每批落库并写检查点的记录数，默认 500 |
| `--checkpoint PATH` | 自定义检查点路径 |
| `--reset-checkpoint` | 忽略并删除已有检查点，从文件开头重新摄取 |

坏记录以退出码 `1` 失败，stderr 给出位置与安全恢复偏移；检查点文件损坏以
退出码 `2` 失败。修复来源后**用同样的命令重跑**即可从检查点恢复。

## 受控大样本演示

`demo/run_demo.py` 生成隔离临时目录中的受控大样本（默认 120 MB，正中插入
一条等长可修复的坏记录），证明三件事：内存有界、错误精确定位、修复后从
检查点恰好一次恢复：

```bash
python3 -I demo/run_demo.py                 # 120 MB
python3 -I demo/run_demo.py --mb 250        # 自定义大小
python3 -I demo/run_demo.py --keep          # 保留生成的数据目录
```

实测（64 KiB 分块）：8 MB → 120 MB 文件放大 15 倍，进程峰值 RSS 仅从
约 15.9 MB 增至 17.5 MB。

## 编程接口

```python
from parts_catalog import (
    IncrementalJsonSource, PartsPipeline, PartsStore, CheckpointStore,
)

# 增量接口：逐条拉取，内存有界
source = IncrementalJsonSource("feed.jsonl")
for record in source.iter_records():
    record.sequence, record.start_offset, record.line, record.data

# 带检查点的可恢复管道
cp_store = CheckpointStore("state/checkpoint.json")
source = IncrementalJsonSource("feed.jsonl", checkpoint=cp_store.load())
stats = PartsPipeline(
    source, PartsStore("parts.sqlite3"),
    batch_size=500, checkpoint_store=cp_store,
).run_streaming(currency="USD", source_ref="feed.jsonl")
```

旧的 `JsonFixtureSource(...).read()` 与 `PartsPipeline(source, store).run(...)`
签名继续可用，内部已切换为增量扫描。

## 测试

```bash
# 标准库 unittest（本环境无 pytest 依赖时同样适用，pytest 亦可直接收集）
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v

# 含 80 MB 大文件内存有界性测试
RUN_SLOW_TESTS=1 PYTHONPATH=src python3 -m unittest tests.test_sources.LargeFileMemoryTests -v
```

## Node 门户流程

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```
