# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 运行 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```

## 目录字段归属与合并机制

背景：季度目录复核会上，供应商一条临时描述改写了包装规格引发争议——主数据团队只承认人工签核的规格，采购团队要求新采集的报价立即可用。为此导入链路内置了字段级归属与合并机制。

### 受管字段与来源等级

受管目录字段为 `brand`、`description`、`unit`、`package_quantity`。每条记录带来一个来源（origin），按优先级排序：

| 来源 | 等级 | 含义 |
| --- | --- | --- |
| `manual` | 100 | 主数据团队人工签核的主数据 |
| `feed` | 10 | 供应商门户/文件采集 |

记录可以自带 `"origin"` 与 `"note"` 键（如签核批注），否则使用导入时的 `--origin`（默认 `feed`）。未知来源直接拒绝导入。

### 每个字段的裁决结果

合并是纯函数策略（`merge.decide`），逐字段产出六种结果，全部写入 `field_decisions` 台账供复核：

- `applied`：字段无归属值，接受新值；
- `confirmed`：值相同但来源等级更高（人工签核确认现有值），**所有权转移**到高等级来源——签核从此锁定该字段；
- `unchanged`：值相同且无需转移所有权，空操作；
- `skipped`：记录未断言该字段（原始报文里根本没有这个键），**绝不擦除**现有值；
- `overridden`：值冲突且新值胜出（高等级覆盖低等级，或同级最新记录生效），留痕待复核；
- `conflict`：值冲突且新值被拒绝（低等级不得覆盖高等级），保留原值，留痕待复核。

当前归属存于 `field_provenance`（每个字段谁拥有、来自哪个来源、签核批注）；`parts` 表只是归属值的物化视图。从未被任何来源断言的字段在 `parts` 行中显示契约默认值，且没有 provenance 行——复核者一眼能看出它"无人认领"。

### 报价独立性

报价（价格、交期、库存）是**独立的供应商事实**，不参与目录裁决：`quotes` 表按 `(canonical_number, supplier, source_ref)` 幂等写入，合并逻辑从不触碰。即使一条采集记录的目录字段被全部拒绝（如临时改写的包装规格），它的报价仍然完整入库、立即可用——这正是采购团队的诉求。人工签核记录是主数据而非供应商事实，不产生报价。

### 优先级与冲突处理的取舍

1. **人工签核绝对优先**：低级来源永远不能覆盖高级来源。这直接回应了事故——签核后的包装规格不会再被临时描述改写。代价是供应商对签核字段的合法变更也必须走一次人工复核，这是主数据团队明确接受的。
2. **签核确认即接管（confirmed）**：签核与现有值一致时也转移所有权。否则"确认过的字段"仍属 feed，会被下一条同级记录改写，签核形同虚设。
3. **同级冲突最新值生效 + 留痕**：两条 feed 记录互相矛盾时，后到的生效并记录 `overridden`。选最新值而非先到先得，是因为供应商描述/包装会合法变更，全部转人工不可扩展；需要钉死某字段时，用一次人工签核即可将其升级为 `manual` 并阻断后续覆盖。
4. **缺字段跳过而非默认**：原始报文里没有的键视为"未断言"，规范化阶段的契约默认值（`EA`、`1`、`""`）绝不冒充供应商事实去覆盖已有归属。
5. **台账"同一断言首次裁决生效"**：`field_decisions` 以 `(型号, 字段, 新值, 新值来源)` 为键、`INSERT OR IGNORE` 写入。相同断言的重复导入是空操作，保证**二次导入零漂移**；代价是同一断言在不同历史时刻的后续裁决不覆盖首条记录（当前状态以 `field_provenance` 为准）。台账不存墙钟时间，换取完全确定的重放；如需完整事件流，可按 run_id 扩展。

### 复核演示

用人工签核记录分别叠加完整、缺字段、相互矛盾的采集记录：

```bash
DB=/tmp/review.sqlite3
for f in manual_signoff feed_complete feed_missing_fields feed_conflicting; do
  PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/$f.json --database $DB
done
PYTHONPATH=src python3 -m parts_catalog.cli --database $DB --review "AB-120 / 4"
```

`--review` 输出该型号的当前目录行、逐字段归属、完整裁决台账和独立保存的报价。把上面四个 fixture 再导入一遍，所有表逐行不变（`tests/test_pipeline_merge.py` 中的 `test_manual_overlay_then_contradictory_feeds_and_reimport_no_drift` 对四张表做全量快照断言）。
