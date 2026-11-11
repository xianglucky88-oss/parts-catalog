# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 运行 fixture

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

## 导入工作单元与事务语义

`PartsStore` 是唯一的连接拥有者，调用方无法直接写库或提交。每次导入通过
`store.import_batch()` 开启一个显式工作单元（`ImportBatch`）：

- 进入 `with` 块时执行 `BEGIN IMMEDIATE`（事务开始点）；
- 仅当 `with` 块无异常退出时才 `COMMIT`（提交条件），提交失败也会回滚；
- 块内任何异常都会 `ROLLBACK`，上一批已提交数据保持可读、不受影响；
- 工作单元结束后（无论提交或回滚）即关闭，继续写入抛出
  `StorageStateError`，半批数据不可能被误提交；
- `PartsStore.close()` 幂等，并会回滚任何未完成的批次。

因此夜间补采任务中断后，直接重跑同一 fixture 即可：失败批次不会留下任何痕迹。

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```
