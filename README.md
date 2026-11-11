# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 运行 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

## 来源适配器边界

每家供应商的原始结构由独立适配器负责转换，核心管道只接受统一的 `SupplierPart`：

- `JsonFixtureSource` 把每条来源记录包装成 `RawPartEnvelope`，携带来源文件与记录定位符（如 `$.records[1]`）。兼容旧的记录列表（按记录取 `supplier`，无版本）与带契约版本的文档（`{"supplier", "contract_version", "records"}`）。
- `AdapterRegistry` 按 `(supplier, contract_version)` 选择适配器；未显式声明版本的记录回落到该供应商注册的默认契约，未知供应商或未知版本直接拒绝。
- 适配器把各自原始形状转换为 `SupplierPart` 后，管道再校验统一契约（非空零件号/品牌、正的包装数量等）。
- 整批记录全部通过校验后才写入 SQLite；任一记录违约都抛出 `SourceBatchRejectedError`，错误携带 supplier、契约版本与记录定位符，采集故障可精确归因，且拒绝发生时存储保持零写入。
- `quotes.source_ref` 形如 `<source>#$.records[i]`，可回溯到具体原始记录。

第二家供应商（Vertex，契约 2.1，零件号/品牌/库存为嵌套字段）的 fixture：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload_nested.json --database /tmp/parts.sqlite3
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```
