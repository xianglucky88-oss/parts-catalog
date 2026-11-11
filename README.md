# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 运行 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

## 门户执行层（Node.js）

供应商门户把频繁登录视为风险行为，因此执行层以**会话池**方式运行：有限数量的已登录会话被反复用于多个查询，而不是每次查询都重新登录。每个查询携带 `requestId`，结果以 JSONL 逐行输出并始终归属到对应 `requestId`：

```bash
node automation/portal_runner.mjs --query AB-120 --query ZZ-9 --request-id req-1 --request-id req-2
# {"requestId":"req-1","status":"ok","data":{...}}
# {"requestId":"req-2","status":"ok","data":{...}}
```

- **会话失效**：驱动抛出 `SessionInvalidError` 时，池会关闭旧会话、重新登录并在新会话上重试该查询（次数有限），重试耗尽后按 `SESSION_INVALID` 失败并带上原 `requestId`。
- **单查询超时**：超时的查询以 `TIMEOUT` 失败并带上 `requestId`，其会话被回收重建，不影响其他查询。
- **关闭竞态**：`close()` 可重复调用；排队中的查询立即以 `POOL_CLOSED` 失败（含各自 `requestId`），进行中的查询运行至完成（受自身超时约束），所有会话恰好关闭一次。
- **凭据卫生**：账号与密码只用于登录表单，所有日志与错误对象在离开执行层前都经过脱敏，绝不包含凭据。
- **dry-run**：`--dry-run` 用本地 fake 驱动跑完整会话编排（无需浏览器与环境变量），输出同样按 `requestId` 归属：

```bash
node automation/portal_runner.mjs --dry-run --query AB-120 --request-id req-1
```

真实运行时通过环境变量提供配置（见 `automation/.env.example`），禁止将账号或 token 写入源码。查询也可以用 `--queries-file queries.json` 提供（字符串或 `{id, query}` 数组）。退出码：全部成功为 0，任一查询失败为 1，用法/配置错误为 2。

测试使用本地 fake 驱动验证会话复用、并发隔离与资源回收，不需要浏览器：

```bash
cd automation && npm test
```
