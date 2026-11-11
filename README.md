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

## 数据工程验证入口（CI）

`scripts/verify_data_engineering.py` 是统一的数据工程验证入口，一条命令按序编排四个阶段：

1. **deps** — 依赖准备：校验 Python（>=3.9）与 Node.js（>=20）版本；有 pip 时安装 `.[test]`，无 pip 时退化为导入校验（项目运行时零依赖）；`automation/node_modules` 缺失时执行 `npm install --ignore-scripts`（dry-run 不需要下载浏览器），已存在则跳过，保证离线可重复。
2. **python-pipeline** — 用离线 fixture 连续跑两遍 Python 管线并断言结果一致（幂等），随后校验 SQLite：行数非空、canonical 主键非空、quotes→parts 引用完整；若安装了 pytest 则顺带运行单元测试（嵌套调用时自动跳过，避免递归）。
3. **node-dry-run** — 以 `--dry-run` 执行供应商门户流程并校验其输出契约；CI 环境变量中的真实 `PORTAL_*` 配置原样透传给子进程，但绝不写入日志。
4. **cleanup** — 删除临时 SQLite 工作目录；即使前面阶段失败也会执行。

```bash
python3 scripts/verify_data_engineering.py
```

- **退出码传播**：任一阶段失败时，入口以该阶段的退出码退出（子进程退出码原样传播，信号映射为 128+N；127=命令缺失，124=超时，2=运行时前置条件不满足），并在输出中标注失败阶段，例如 `RESULT: FAILED - stage 'python-pipeline' exit code 1`。
- **输出脱敏**：所有子进程输出在转发前按环境变量名（PASSWORD/TOKEN/SECRET/KEY 等）及 URL 内嵌凭据做掩码，日志中不会出现密码或 token。
- **可重复、离线**：只使用仓库内 fixture，可反复运行且不留临时文件；`tests/test_verify_entrypoint.py` 用离线 fixture 固化了这些性质（连续两次运行均成功、失败码传播、输出无秘密、临时目录被清理）。

真实门户运行（非 dry-run）需另行 `npx playwright install` 下载浏览器，并通过环境变量提供 `PORTAL_*` 配置。
