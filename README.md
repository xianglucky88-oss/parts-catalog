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

## 供应商实体解析登记册

并购交接资料（旧门户账号、新集团名称、地区别名）通过 `--handover` 进入登记册。登记册将四类信息分离存储：`supplier_observations`（原始名称 + source_ref，从不改写）、`supplier_aliases`（别名主张及其依据）、`supplier_entities`（稳定实体身份）、`supplier_decisions` / `supplier_evidence`（审计轨迹）。每次判断处于 `resolved` / `pending` / `conflict` 之一；只有充分证据（已确认别名、实体显示名精确匹配、已解析名称的大小写/空白变体）才能自动解析，相似名称与冲突主张保留给人工处置。决定可撤销（`--revoke-decision`），撤销会留下人工 `pending` 挂起而非静默重判；重复接收同一资料不会累积观测、别名或证据。

```bash
PYTHONPATH=src python3 -m parts_catalog.cli \
  --fixture fixtures/supplier_handover.json \
  --handover fixtures/supplier_handover.json \
  --database /tmp/parts.sqlite3 \
  --supplier-report
```

报价行的 `supplier` 保留原始名称，`supplier_entity_id` 记录解析出的稳定实体身份；`pending` / `conflict` 的观测不写实体身份，等待人工处置。
