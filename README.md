# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 证据账本（字段级转换溯源）

每条目录记录与报价在写入 SQLite 时，同时向**追加式哈希链账本**写入证据，回答合规问题：最终报价由哪段输入、经哪些规则、以什么顺序、把什么改成了什么。

### 表结构（新增表均为附加，旧表 `parts`/`quotes` 结构与语义不变）

- `import_runs`：一次逻辑导入一行。`run_id` 由输入内容哈希、`source_ref`、币种、规则集版本确定性派生（重放同一输入得到同一 `run_id`），并记录输入摘要 `input_hash`、规则集版本、记录数与状态。
- `evidence_ledger`：每个实体写入（part 或 quote）一行，全局单调 `seq`，包含
  - 输入绑定：`input_hash`、记录在导入中的位置 `raw_index`、单条原始记录哈希 `raw_record_hash`；
  - 规则绑定：`rule_set_version` 与 `rule_ids`，`transforms` 逐字段记录 `(rule_id, field_name, before, after)`；
  - 写入前后实体：`before_snapshot` / `after_snapshot`（首次写入 before 为 `NULL`，覆盖写入保留旧值）；
  - 顺序与防篡改：`prev_hash` 指向上一条目的 `entry_hash`，`entry_hash` 覆盖本行全部字段（含 prev_hash），形成从 `GENESIS` 出发的 SHA-256 哈希链。

### 事务与幂等语义

- 一次导入的业务事实、证据条目、`import_runs` 标记在**同一个 SQLite 事务**内提交；提交前任何异常都会整体回滚，数据库里不会出现"未完成却像完成"的记录。
- 相同输入重放：`run_id` 已存在时整单跳过，不新增任何业务事实或证据（`replayed: true`, `entries_written: 0`）。
- 打开旧版本数据库时只 `CREATE TABLE IF NOT EXISTS` 新增两张账本表，历史行原样可读，并可在同一库中继续新导入。

## 命令

```bash
# 导入（保留旧的无副命令调用形式，等价于 import）
PYTHONPATH=src python3 -m parts_catalog.cli import \
  --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3

# 审计：重算全部哈希、检查序号连续性、前后快照与当前实体一致性、run 绑定
# 退出码 0 = 通过，2 = 发现问题；附 --fixture 可把外部输入重新绑定到原始记录哈希
PYTHONPATH=src python3 -m parts_catalog.cli audit --database /tmp/parts.sqlite3 \
  --fixture fixtures/supplier_payload.json

# 单实体追溯：按发生顺序列出每次写入的规则、逐字段转换与前后快照
PYTHONPATH=src python3 -m parts_catalog.cli trail --database /tmp/parts.sqlite3 \
  --type part --key AB1204
PYTHONPATH=src python3 -m parts_catalog.cli trail --database /tmp/parts.sqlite3 \
  --type quote --canonical AB1204 --supplier Northstar \
  --source-ref fixtures/supplier_payload.json
```

审计可定位的问题与影响范围（每条 finding 给出首个断裂 `seq` 和 `blast_radius`，即从该序号起链上所有不可信条目）：

| 代码 | 含义 |
| --- | --- |
| `hash.tampered` | 行内容被改，重算哈希与存储的 `entry_hash` 不符（篡改） |
| `chain.link_broken` | `prev_hash` 接不上前一条（重排/改写） |
| `sequence.gap` | `seq` 不连续（删除导致的缺失） |
| `snapshot.discontinuity` | 同实体后一条的 before 不等于前一条的 after（绕过管道的改写） |
| `state.drift` | 账本最后 after 快照与业务表当前行不一致（业务表被直接改动） |
| `run.unknown` / `run.evidence_missing` / `run.input_mismatch` | 证据缺失或输入摘要与 run 记录不符 |
| `input.hash_mismatch` / `input.unrecorded` | 外部输入文件无法重新绑定到账本中的原始记录哈希 |

## 五类约束的落实（可复现演示）

```bash
PYTHONPATH=src bash automation/demo_evidence.sh
```

1. **正常导入**：业务行与 4 条证据（2 记录 × part/quote）同事务提交，`audit` 通过；`trail` 显示同一报价键的两次写入（18.50 → 18.75）及各自规则。
2. **乱序输入**：逆序 fixture 的 `input_hash`/`run_id` 与原导入不同，作为独立 run 入账且审计通过；每条证据的 `raw_index` 保留其在本导入中的原始位置。
3. **重放**：同一输入再次导入返回 `replayed: true`、`entries_written: 0`，四张表行数逐字节不变。
4. **篡改**：改快照（`hash.tampered`）、删条目（`sequence.gap` + `chain.link_broken`）、物理重排（`chain.link_broken`）、直接改业务表（`state.drift`）全部被定位，并给出从首断点到链尾的影响范围。
5. **中断写入**：`import --simulate-fault` 在提交前注入异常，事务回滚后四张表计数均为 0，CLI 显式返回 `rolled_back`（退出码 4）；随后同一导入可正常成功。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```
