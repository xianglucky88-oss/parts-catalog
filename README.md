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
