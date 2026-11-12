# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 报价准入边界（财务比较服务前置）

报价在进入财务比较集合之前必须通过 `parts_catalog.admission` 的准入闸，规则只有两类：

1. **精确有限金额**：仅接受 `Decimal`、`int` 或精确十进制字符串。拒绝 `float`（二进制浮点不精确）、`NaN`/`Infinity`（非有限）、负数以及早年迁移残留的价格文本（如 `call for price`，不可解析）。
2. **受控货币代码**：先修复格式变体（首尾空白、大小写，如 `" usd "` → `USD`），再校验为受控白名单 `USD/EUR/GBP/JPY/CNY` 内的三字母 ISO 代码；内部空白与未知代码（如 `CHF`）一律拒绝。

准入决定携带**原始值**与**拒绝理由代码**，由存储层写入 `quote_admission_ledger` 留痕。财务侧只能通过 `eligible_quotes` 视图读取比较集合——该视图在数据库层内连接留痕表并过滤 `admitted = 1`，不合格记录在物理上无法混入。

本边界**不做**汇率换算，也**不做**包装/单位换算。

### 运行 fixture（写入路径，逐条过闸）

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/quote_admission_demo.json --database /tmp/parts.sqlite3
```

### 迁移已有 SQLite 记录（可重复执行）

对迁移前已存在于 `quotes` 表、尚无留痕的记录重新过闸；原始 `quotes` 行不被修改或删除，原始值与拒绝理由落在留痕表：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --admit-existing --database /tmp/parts.sqlite3
```

### 留痕与查询

- `eligible_quotes` 视图：唯一的财务比较集合，只含受控货币 + 精确有限金额。
- `quote_admission_ledger` 表：全量决定（`admitted`、规范化值、`raw_*` 原始值、`reason_code`/`reason`、`decided_at`）。
- 代码入口：`PartsStore.admit_and_store()`（新写入）、`PartsStore.admit_existing_quotes()`（旧库迁移）、`PartsStore.eligible_quote_rows()` / `rejected_quote_rows()` / `admission_rows()`。

## 运行原始 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```

## 测试

```bash
PYTHONPATH=src python -m pytest tests/
```
