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

## 离线测试

全部为离线测试：Python 侧写入临时 SQLite，Node 侧用内存 fake 替代 Playwright 浏览器，不访问真实站点，也不需要任何凭据。

Python 跨层契约测试（fixture → 规范化 → pipeline → SQLite；stdlib 即可运行，安装 pytest 后 `pytest` 同样收集）：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Node 采集端边界测试（成功、空结果、浏览器异常）：

```bash
npm --prefix automation test
```

当前基线：Python 14 项中 4 项失败、Node 11 项中 2 项失败，失败项即测试暴露出的实现缺陷（报价静默覆盖、缺字段报错类型、CLI 输出非 JSON、空结果未结构化、关闭异常掩盖根因），修复前不应视为回归通过。
