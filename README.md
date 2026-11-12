# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化、版本化业务质量策略和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 运行 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```

## 版本化业务质量策略（policy v1.0）

来源适配器只负责把不同门户结构转换为统一的 `SupplierPart`；导入入口
（`parts_catalog.quality.QualityPolicy` + `PartsPipeline`）随后对已适配记录执行业务质量判定。
该模块不选择来源适配器、不解析嵌套字段、也不判断原始契约版本。每条记录得到三种结论之一：

| 结论 | 触发条件 | 落库行为 |
| --- | --- | --- |
| `accepted` | 关键身份齐全、字段覆盖率 ≥ 50%、无适配错误、无相互矛盾值 | 写入 `parts` 目录与 `quotes` 报价（沿用原语义） |
| `quarantined` | 身份可用但字段覆盖率低于 50%、数值无法适配，或同供应商/同型号的结构化字段互相矛盾 | 写入 `quality_decisions`，不进业务表 |
| `rejected` | 关键身份（`supplier` / `part_number` / `brand`）缺失或规范化后无可用值 | 写入 `quality_decisions`，不进业务表 |

规则要点：

- **关键身份可用性**：`supplier`、`part_number`、`brand` 缺失、空白或规范化后为空（如型号全是符号）直接 `rejected`。
- **字段覆盖率**：以 `description`、`unit`、`package_quantity`、`lead_time_days`、`stock_status`、`price`
  六个业务字段计算；空串、`N/A`、`unknown` 等视为不可用。缺失 1–3 个字段（覆盖率 ≥ 0.5）仍可接受，
  缺失 4 个及以上（覆盖率 < 0.5）隔离，并给出 `low_coverage` 原因。
- **相互矛盾值**：同一供应商 + 同一规范型号下，`brand`、`unit`、`package_quantity`
  出现不同值即判定矛盾并隔离冲突双方；价格、交期、库存属于报价属性，允许不同报价并存；
  `description` 为自由文本，不作为矛盾判据。
- **字段级原因**：每个结论附带 `field / code / message / value` 四元组，随隔离/拒绝记录一起持久化。
- **质量剖面**：按供应商汇总总数、三类结论计数、平均覆盖率与缺失字段计数，存入 `quality_profiles`。
- **版本化**：每条结论与剖面都带 `policy_version`（当前 `1.0`），内容指纹
  （SHA-256，含策略版本）作为隔离表主键，相同输入重放只做 upsert，
  不会重复生成目录、报价或隔离记录。

### 数据表

- `parts` / `quotes`：原有业务表，语义与主键不变。
- `quality_decisions`：隔离与拒绝记录（含字段级原因 JSON、原始记录、内容指纹）。
- `quality_profiles`：按 `(supplier, policy_version, source_ref)` 唯一的供应商质量剖面。
