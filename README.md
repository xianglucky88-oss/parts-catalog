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

## 包装量纲与单位成本

询价场景里，甲供应商按“每件”报价、乙供应商按“一箱 100 件的总价”报价，两个金额本身无法直接比较。本能力在**已通过金额校验的报价**之上工作，只在数量、单位与换算关系都可由报价本身证明时给出精确单位成本，其余情况一律返回带原因码的可判定结果，绝不做猜测性换算。

- 包装数量（`package_quantity`）缺失时显式保存为 `NULL`，不再默认成 1；报价行同时保存计量单位与包装数量。
- 计量基础（`measurement_basis`）：`per_each`（单件计价，换算系数 1）、`per_package`（整盒/整箱计价，仅当包装数量已知时按 `price / package_quantity` 精确换算）、`incompatible_dimension`（如 KG/MM 等质量、长度量纲，与“件”之间无可证明换算）。
- 无法给出精确单位成本时保存原因码（`reason`）：`price_missing`、`unknown_unit`、`package_quantity_missing`、`incompatible_dimension`；跨报价比较时若币种不同则记 `currency_mismatch`。
- 所有计算使用 `Decimal`，模块精度 28 位；单位成本、换算系数与文字化换算依据（`conversion_basis`）一并落库到 `unit_costs` 表，保证可审计。
- `compare_unit_costs` 只在全部报价均为精确单位成本且币种相同时输出排名；否则不排名，并逐户保留原因。

运行包装量纲 fixture：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/unit_cost_payload.json --database /tmp/unit-cost.sqlite3
# {'processed': 6, 'exact_unit_costs': 2, 'incomparable': 4, ...}
```

明确的范围边界：本模块**不包含**货币清洗、历史货币迁移或任何汇率/外汇换算功能。货币仅作为标签参与同币种比较，不同币种不会被折算，只会被标记为不可比较。
