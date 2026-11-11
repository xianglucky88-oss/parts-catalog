# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 跨运行时交接契约

采集端（Node.js）与导入端（Python）之间通过**批次信封**交接，机器可读契约见：

- `contract/batch-envelope.schema.json` — 采集端输出的批次信封；
- `contract/import-report.schema.json` — 导入端打印到 stdout 的导入报告。

信封字段：`contract_version`、`batch_id`（批次标识，幂等键）、`supplier`（供应商）、`queried_at`、`query.part_number`（查询物料）、`results`（原始结果数组，每项含 `status`: `ok` 携带原始 `part`,`error` 携带结构化 `error.code/message`）。

导入端对每个批次输出恰好一个 JSON 报告，数据管道依据 `outcome` 字段分支：

| outcome | 含义 |
| --- | --- |
| `imported` | 全部结果导入成功 |
| `empty` | 空结果批次（合法状态，已入账） |
| `partial` | 半批失败：部分结果导入，失败行在 `errors` 中逐条列出 |
| `failed` | 有结果但全部失败，未写入任何目录/报价数据 |
| `duplicate` | 重复提交：`batch_id` 已入账，本次不重复导入 |

每个已处理批次都会写入 SQLite 的 `ingestion_batches` 台账表（含状态与计数），重复提交由 `batch_id` 主键保证幂等；需要重试时采集端必须生成新的 `batch_id`。报价行的 `source_ref` 即批次标识，可逐行追溯回批次。

## 运行 fixture

手工导入（原有行为，纯 JSON 数组负载）：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

批次信封导入（演示半批失败：1 条成功 + 1 条抽取失败）：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --batch fixtures/batch_envelope.json --database /tmp/parts.sqlite3
# → {"batch_id": "northstar-2026-11-11-0001", "outcome": "partial", "received": 2, "imported": 1, "failed": 1, ...}
# 再执行一次 → "outcome": "duplicate"，数据不会重复写入
```

端到端交接演示（Node 采集 → Python 导入）：

```bash
node automation/portal_runner.mjs --dry-run --out /tmp/batch.json
PYTHONPATH=src python3 -m parts_catalog.cli --batch /tmp/batch.json --database /tmp/parts.sqlite3
# dry-run 输出空结果信封 → "outcome": "empty"
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置（见 `automation/.env.example`，新增 `PORTAL_SUPPLIER` 与可选的 `BATCH_ID`），缺少配置会直接报错退出，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```

## 测试

```bash
PYTHONPATH=src python3 -m pytest tests/
```
