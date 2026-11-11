# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 运行 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
# 等价的显式子命令
PYTHONPATH=src python3 -m parts_catalog.cli ingest --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```

## 交换快照协议（Parts-Catalog Snapshot, PCSv1）

离线审查环境不直接复制正在运行的 SQLite 文件，而是交换**自描述、可校验、可重放**的快照包。

### 产物布局

导出（`export-snapshot`）产生两个必须一起传递的文件：

| 文件 | 作用 |
| --- | --- |
| `pcs-<batch>-<UTC时间>.pcsnap.tar` | 未压缩 USTAR 归档，固定三个成员：`manifest.json`、`catalog.json`、`quotes.json` |
| `pcs-...pcsnap.tar.sha256` | 整包 SHA-256 侧车（传输层信任根，`sha256sum -c` 可独立验证） |

`manifest.json` 携带：

- `format` / `format_version`：协议魔数与格式版本（当前 `1`）
- `batch_id` / `source_ref` / `snapshot_id` / `produced_at`：来源批次与溯源元数据
- `content`：每个数据文件的 SHA-256 与记录计数
- `record_checksums`：**逐条记录**的 key 与 SHA-256，可精确定位损坏/篡改的实体
- `database_identity`：导出方 SQLite 版本

归档按固定顺序、固定元数据（mtime=0、uid/gid=0）和规范化 JSON 写出；相同数据重复导出字节完全一致，便于跨环境比对。

### 三级完整性校验

1. 侧车整包 SHA-256：证明收到的字节与发出的字节一致（`--require-sidecar` 可强制要求）
2. manifest 文件级哈希：证明 `catalog.json` / `quotes.json` 未被篡改
3. 逐条记录哈希 + 字段模式校验 + 报价到目录的引用完整性：定位具体损坏记录

### 导入决策（全部为 fail-closed）

| 情形 | 决策 | 错误码 |
| --- | --- | --- |
| 未知/更高/缺失 `format_version` | **整包拒绝**，不猜测前后向兼容 | `unknown_version` |
| 快照内重复实体（同主键 part 或 quote） | **整包拒绝**，不做部分加载或静默覆盖 | `duplicate_entity` |
| 记录哈希不符、字段非法、JSON 损坏、悬挂报价引用 | **整包拒绝**，detail 给出表名与记录 key | `corrupt_record` |
| 侧车/文件哈希不符、tar 结构非法或含路径穿越成员 | **整包拒绝** | `integrity_failure` |
| 全部校验通过 | 在同目录临时库构建新状态，`PRAGMA integrity_check` 通过后 `os.replace` **原子切换**；旧库保留为 `.pre-import.<时间>.bak` | — |

目标数据库在校验全部通过前不会被创建或修改；任何拒绝都使其字节保持不变，临时库立即清理。导入后的库含 `snapshot_meta` 表，记录批次、版本、导入时间与整包哈希。

### CLI

```bash
# 在线侧：从只读一致事务导出
python3 -m parts_catalog.cli export-snapshot \
  --database online/parts.sqlite3 --output-dir outbox \
  --batch-id BATCH-2026-Q4-007 --source-ref portal://northstar/2026Q4

# 离线侧：只验证不落地
python3 -m parts_catalog.cli verify-snapshot outbox/<bundle>.pcsnap.tar --require-sidecar

# 离线侧：验证通过后原子切换数据库（拒绝时退出码 2，目标不变）
python3 -m parts_catalog.cli import-snapshot outbox/<bundle>.pcsnap.tar \
  --database offline/review.sqlite3

# 跨环境比对业务行指纹（两环境输出相同即状态可复现）
python3 -m parts_catalog.cli fingerprint --database online/parts.sqlite3
python3 -m parts_catalog.cli fingerprint --database offline/review.sqlite3
```

### 验证

```bash
# 标准库单元测试（环境无 pytest 时也可运行）
PYTHONPATH=src python3 -m unittest tests.test_snapshot -v

# 端到端演练：一次有效交换 + 五次故意篡改/冲突交换，
# 覆盖可复现性、原子拒绝与目标库字节不变性（使用 -I，数据目录视为不可信）
python3 -I scripts/exchange_demo.py
```
