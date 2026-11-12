# Parts Catalog

工业备件供应商目录与报价采集的独立实现。Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。

## 供应商活动视图的发布语义

事故背景：夜间采集只跑完半数查询时，未返回的供应商曾被当成"无报价"写进采购可见视图，上一轮仍然有效的完整报价被覆盖。为此导入流程按一轮采集（run）落实以下语义：

### 一轮采集启动时固定查询范围

`begin_run` 把本轮要执行的查询清单固化为 `(supplier, canonical_number)` 单元格集合（`run_scope`）。本轮采集只能影响这些单元格；清单之外的回传一律拒绝（`OutOfScopeError`）。同一 `run_id` 用相同范围重复启动是安全的幂等操作；范围或来源冲突则拒绝。

### 何时能替换采购可见视图

回传先进入暂存区（`staged_quotes`），采购不可见。只有当范围内**每个查询都有明确结果**（有报价，或供应商明确回报无报价——`price` 为 NULL 且缺货）时，`complete_run` 才在一个事务里按单元格原子替换可见视图（`quotes`），范围之外的单元格保持原发布不变。

### 何时必须回退到上一份完整结果

只要本轮范围未被完整覆盖（崩溃、只到了半数查询、批次校验失败），`complete_run` 抛出 `IncompleteRunError` 并列出缺口，可见视图**保持上一份完整发布**——未重新查询的供应商绝不被标成无报价。

### 未完成工作怎样留下可安全重试的状态

- 暂存结果随每批回传持久化，进程崩溃不丢失；
- `pending_queries` 精确列出还差哪些查询，重试只需补这些；
- 同一 `run_id` 可恢复续跑（`failed` 状态自动回到 `collecting`），已暂存数据保留；
- 已发布的 run 不可变，新一轮采集必须使用新的 `run_id`。

### 三种不产生歧义可见结果的事件

- **重复回传**：按 `(run_id, supplier, canonical_number)` 幂等覆盖，不重复、不影响可见视图；重复执行已完成的一轮只报告 `already_published`。
- **开始新一轮采集**：只写入 run 与范围记录，可见视图在新一轮完整发布前保持原样。
- **来源过期**：发布可携带 `valid_until`，或用 `--expire-source` 使某来源的全部已发布报价到期。过期行**保留并显式标注 `expired`**——既不冒充有效，也不凭空消失而被误读为无报价。

## 运行 fixture

一次性导入（负载自带范围，校验通过即发布）：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/supplier_payload.json --database /tmp/parts.sqlite3
```

多批回传的一轮采集必须显式命名 `run-id`，并用 `--scope` 固定查询清单：

```bash
# 上一轮完整采集（基线，带有效期）
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/baseline_delivery.json \
  --scope fixtures/scope_manifest.json --run-id night-0 \
  --valid-until 2099-01-01T00:00:00+00:00 --database /tmp/parts.sqlite3

# 夜间只跑完半数查询：状态为 collecting 并列出 pending，可见视图仍是 night-0
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/partial_delivery.json \
  --scope fixtures/scope_manifest.json --run-id night-1 --database /tmp/parts.sqlite3

# 用同一 run-id 补传剩余查询，范围覆盖后本轮才原子发布
PYTHONPATH=src python3 -m parts_catalog.cli --fixture fixtures/remaining_delivery.json \
  --scope fixtures/scope_manifest.json --run-id night-1 --database /tmp/parts.sqlite3
```

查看采购此刻应相信的报价视图（每行带 `current`/`expired` 标注）：

```bash
PYTHONPATH=src python3 -m parts_catalog.cli --view --database /tmp/parts.sqlite3
```

运维命令：

```bash
# 某一轮还差哪些查询（可安全重试的缺口清单）
PYTHONPATH=src python3 -m parts_catalog.cli --pending night-1 --database /tmp/parts.sqlite3

# 使某来源的全部已发布报价到期（显式标注，而非删除）
PYTHONPATH=src python3 -m parts_catalog.cli --expire-source fixtures/scope_manifest.json --database /tmp/parts.sqlite3
```

故障输入演示：`fixtures/faulty_payload.json` 含非法价格与非法包装数量，整批被拒绝（退出码 2），本轮保持可重试状态，修正后用同一 `run-id` 重传即可。

Node 流程只在明确提供 `--dry-run` 时执行模拟；真实运行时通过环境变量提供配置，禁止将账号或 token 写入源码：

```bash
node automation/portal_runner.mjs --dry-run
```

## 测试

```bash
python3 -m pytest
```
