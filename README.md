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

## 报价决策投影

采购审批不能依赖“只取最小数字”的查询。`parts_catalog.decision` 在已持久化的
SQLite 数据之上重建一个**确定性、可复算、机器可读**的报价决策投影。投影以只读
方式（`mode=ro`）打开数据库，导入链路（fixture → 规范化 → `PartsStore`）保持不变。

```bash
# 仅投影已有数据（stdout）
PYTHONPATH=src python3 -m parts_catalog.cli --project --database /tmp/parts.sqlite3
# 或写入文件
PYTHONPATH=src python3 -m parts_catalog.cli --project --database /tmp/parts.sqlite3 --output /tmp/decision.json
```

本地多场景样例（多供应商、同价并列、缺价、缺货、无合格候选）：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/decision_cases.json --database /tmp/demo.sqlite3
PYTHONPATH=src python3 -m parts_catalog.cli --project --database /tmp/demo.sqlite3 --output /tmp/demo.json
```

### 确定性保证

* 所有排序都在 Python 内使用**全序键**显式完成，SQLite 的物理行序/读取顺序无法影响结果；
  对同一数据以任意插入顺序重建，投影字节级一致。
* 价格按 `Decimal` 精确比较与序列化（无二进制浮点），落选者的 `price_delta` 是精确差额文本。
* 合格候选的排序键：价格升序 → 交期升序（未知交期排最后）→ 供应商名 → source_ref，
  因此同价记录总有稳定、可解释的赢家。

### 不能参与决策 vs 真正落选

`ineligible` 中的记录无法参与比较，带固定原因码，与真正的落选者严格分开：

| reason_code | 含义 |
| --- | --- |
| `price_missing` | 缺价（NULL），无法比价 |
| `invalid_price` | 已存储的价格无法解析为数字 |
| `unavailable_stock` | 库存状态为 `unavailable`（再便宜也不可采购） |
| `currency_mismatch` | 币种与决策币种不同，无汇率不可比较 |

`considered` 中才是参与决策的合格候选：中选者 `outcome="selected"`；落选者
`outcome="lost"`，原因为 `higher_price`（附 `price_delta`）或 `tie_broken`
（附 `tie_broken_by`，即决定并列胜负的那一个维度）。所有候选都不合格的零件
输出 `status="no_eligible_candidate"` 且 `recommendation=null`；没有任何报价的
零件同样会出现在投影中。

### 输出结构

```json
{
  "schema_version": "1.0",
  "decision_currency": "USD",
  "decisions": [
    {
      "canonical_number": "AB1204",
      "status": "recommended",
      "candidate_count": 5,
      "eligible_count": 3,
      "ineligible_count": 2,
      "recommendation": { "supplier": "...", "price": "17.25", "...": "..." },
      "ineligible": [ { "supplier": "...", "reason_code": "price_missing" } ],
      "considered": [
        { "rank": 1, "outcome": "selected", "reason_code": null, "...": "..." },
        { "rank": 2, "outcome": "lost", "reason_code": "higher_price", "price_delta": "1.25" }
      ]
    }
  ]
}
```

## 测试

测试基于标准库 `unittest`（同时兼容 pytest）：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
