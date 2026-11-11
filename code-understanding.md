# parts-catalog 行为基线（供应商门户采集并入前冻结）

> 冻结对象：提交 `0dfb631`（Initial parts-catalog base snapshot），分支 `task/parts-catalog-t01`。
> 方法：只读审计全部业务源码，并在仓库外的隔离目录（`/tmp/baseline-scripts/` 放驱动脚本、`/tmp/baseline-data/` 放临时库与构造输入）用 `python3 -I` 实跑验证；**未修改任何业务代码**，`git status` 全程干净。
> 环境：Python 3.11.2、Node v22.23.4；本环境未安装 `pytest` 与 `playwright`（相关结论以源码加可替代实跑证据给出，见第 8 节）。

---

## 1. 基线总判断（先给结论）

1. **一条物料的完整生命周期只存在于 Python 进程内**：`JsonFixtureSource.read()` 读 JSON → `normalize_part()` 规范化为 `SupplierPart` → `to_catalog_part()` / `to_quote()` 拆成两个冻结 dataclass → `PartsStore.save_part()` / `save_quote()` 以 `INSERT OR REPLACE` 写入**同一个未提交事务** → 全部记录处理完才 `commit()`（`pipeline.py:14-20`）。
2. **重复规范化编号不报错、不计数为冲突**：`count` 统计的是"尝试处理的原始记录数"而非持久化行数（`pipeline.py:18`）。同编号在 `parts` 表必然互相覆盖；在 `quotes` 表是否覆盖取决于复合主键 `(canonical_number, supplier, source_ref)` 是否完全相同。
3. **转换异常（ValueError/KeyError 等）发生在写库之前或事务之中**：本进程没有任何 `catch`，异常直接冒泡致进程退出码 1；由于 `commit()` 永远不会执行，本次运行的全部 DML 随连接关闭**整体回滚**，但建表 DDL 在 `PartsStore` 构造时已单独提交，表结构保留；此前已提交的旧数据不受影响（已实测）。
4. **数据库异常分两类**：构造期异常（如路径不可写）发生在处理任何记录之前，不留数据；运行期 `OperationalError` 同样导致未提交事务整体回滚，已提交行原样保留。
5. **Node.js 门户采集与 Python 导入之间没有任何代码级连接**：全仓检索无 `spawn/exec/subprocess/writeFile`。Node 侧只向 stdout 打印 JSON，且其输出形状（camelCase 单对象/单记录）与 Python 输入契约（snake_case 对象组成的**数组**）不兼容，直接对接实测双双失败。当前唯一"交接"是人工/外部系统落盘的 JSON 文件约定。

---

## 2. 运行时边界：两个互不感知的进程

| 维度 | Node.js（`automation/portal_runner.mjs`） | Python（`src/parts_catalog/`） |
|---|---|---|
| 职责 | 供应商门户浏览器流程（Playwright） | 输入契约校验、型号规范化、SQLite 持久化 |
| 输入 | 环境变量 `PORTAL_URL/USERNAME/PASSWORD/QUERY`（`portal_runner.mjs:3`） | `--fixture` JSON 文件路径、`--database`、`--currency`（`cli.py:10-12`） |
| 输出 | stdout 上的一行 JSON（`portal_runner.mjs:19,38`） | SQLite 文件 + stdout 上的 `{"processed": n, ...}`（`cli.py:17`） |
| 数据库访问 | **无**（不 import 任何 sqlite/数据库库，不写业务文件） | 唯一的 sqlite3 使用点（`storage.py:2,9`） |
| 失败方式 | 缺配置 `throw Error`（`portal_runner.mjs:15`）；playwright 动态导入失败 | 异常冒泡，退出码 1，事务回滚 |
| 配置/密钥 | 仅环境变量，`.env.example` 与 `.gitignore` 保证密钥不入库 | 命令行参数；默认库 `parts-catalog.sqlite3`（`cli.py:11`，已被 `.gitignore:49` 忽略） |

README 第 3 行明确声明同一分工："Python 侧只处理输入契约、型号规范化和 SQLite 持久化；Node.js 侧只负责供应商门户浏览器流程，不直接写业务库。"

---

## 3. 逐层数据流（以 fixture 两条记录实测）

入口 fixture（`fixtures/supplier_payload.json`）含两条记录，原始字段差异如下：

| 字段 | 记录 1 | 记录 2 |
|---|---|---|
| part_number | `"AB-120 / 4"` | `"AB1204"` |
| brand | `"Acme Inc."` | `"ACME"` |
| unit | `"ea"` | `"EA"` |
| stock_status | `"In Stock"` | `"Available"` |
| price / lead_time | `"18.50"` / 4 | `"18.75"` / 5 |

### 第 0 层：读取入口与输入契约 —— `sources.py:15-19`

```python
payload = json.loads(self.path.read_text(encoding="utf-8"))   # sources.py:16
if not isinstance(payload, list):
    raise ValueError("supplier payload must be a list")       # sources.py:17-18
```

- 只校验"顶层是 list"，**不校验元素字段**；空数组 `[]` 合法（实测：`{'processed': 0}`，正常退出，两表建好且为 0 行）。
- CLI 把 `--fixture` 的**原始路径字符串**同时作为 `source_ref` 传入（`cli.py:16`），这一细节在第 5 节会产生可观察的身份差异。

### 第 1 层：规范化 —— `normalize.py:19-38`，产物 `SupplierPart`（`models.py:6-16`，frozen）

逐条规则与实测值：

| 规则 | 代码证据 | 记录 1 → 结果 | 记录 2 → 结果 |
|---|---|---|---|
| 编号大写并删除所有非 `[A-Z0-9]` 字符 | `normalize.py:10-11` `re.sub(r"[^A-Z0-9]", "", value.upper())` | `AB-120 / 4` → **`AB1204`** | `AB1204` → **`AB1204`** |
| 品牌别名表归一 | `normalize.py:6,14-16` | `Acme Inc.` → `ACME` | `ACME` → `ACME` |
| 描述空白折叠 | `normalize.py:32` | `Sealed bearing 20 mm` | `Sealed bearing 20mm` |
| 单位大写 | `normalize.py:33` | `ea` → `EA` | `EA` |
| 库存状态别名表，未命中落 `unknown` | `normalize.py:7,36` | `In Stock` → `available` | `Available` → `available` |
| 价格转 Decimal；`None/""/"N/A"` 放行成 NULL；其余非法值抛错 | `normalize.py:20-24` | `Decimal('18.50')` | `Decimal('18.75')` |
| 包装量 `int()`，小于 1 抛错 | `normalize.py:25-27` | 1 | 1 |
| 交期 `int()`，缺省为 None | `normalize.py:35` | 4 | 5 |
| supplier 直接 `raw["supplier"]`，缺键即 KeyError | `normalize.py:29` | `Northstar` | `Northstar` |

附额外边界实测：`package_quantity: 2.9` 被 `int()` **静默截断为 2**；`lead_time_days: "7"` 字符串被强转为整数 7；无法识别的 `stock_status` 落 `unknown` 字符串；`price: null` 落库为 SQL NULL。

**关键基线事实**：两个不同的原始编号经 `canonical_part_number` 收敛为同一字符串 `AB1204`——碰撞是该函数的设计行为，不产生警告。

### 第 2 层：模型转换 —— `normalize.py:41-46`

- `to_catalog_part()` 只取 5 个目录字段，身份即 `canonical_number`（`normalize.py:41-42` → `models.py:19-26`）。
- `to_quote()` 取 7 个报价字段，`currency.upper()` 归一币种，并把外部传入的 `source_ref` 原样纳入模型（`normalize.py:45-46` → `models.py:28-36`）。
- 两个 dataclass 均 `frozen=True`，转换后不可变。

### 第 3 层：目录身份持久化 —— `storage.py:21-22`，表 `parts`

```sql
canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
unit TEXT NOT NULL, package_quantity INTEGER NOT NULL          -- storage.py:10-13
```
```python
INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)            -- storage.py:22
```

身份只有一列 `canonical_number`。同编号第二次写入 = **整行替换**（SQLite 的 REPLACE 语义：先 DELETE 再 INSERT），没有版本、没有合并、没有历史。

### 第 4 层：报价身份持久化 —— `storage.py:24-25`，表 `quotes`

```sql
PRIMARY KEY (canonical_number, supplier, source_ref)           -- storage.py:14-18
```
```python
INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)     -- storage.py:25
-- price 以 str(Decimal) 存为 TEXT（storage.py:25），实测落库 '18.75'
```

报价身份是**三元组**：同一物料、同一供应商，只要 `source_ref` 不同就被视为不同报价行而并存。

### 第 5 层：事务边界 —— `storage.py:19,27-31` + `pipeline.py:19`

- `PartsStore` 构造时建表并**立即 commit**（`storage.py:19`），所以表结构的存在独立于后续业务事务。
- 之后的 `save_part/save_quote` 均在隐式事务内，只有整轮循环结束后才 `store.commit()`（`pipeline.py:19`）。
- 任何异常导致跳过该 commit 时，`cli.py:18-19` 的 `finally: store.close()` 关闭连接，sqlite3 驱动对未提交事务执行**回滚**。这是本系统唯一的原子性保障：**单次 CLI 运行具有全有或全无语义**，但跨运行没有任何应用层补偿。

### 基线实跑结果（全新空库 + 原始 fixture）

```text
{'processed': 2, 'database': '/tmp/baseline-data/baseline.sqlite3'}
parts  表（1 行）: ('AB1204', 'ACME', 'Sealed bearing 20mm', 'EA', 1)
quotes 表（1 行）: ('AB1204', 'Northstar', 'USD', '18.75', 5, 'available',
                   '/workspace/fixtures/supplier_payload.json')
```

处理了 2 条、只持久化 1 条：记录 2 在两张表上都整体覆盖了记录 1（18.50/4 天/"20 mm" 描述全部消失，无审计痕迹）。

---

## 4. 三类"改变/不改变状态"的判定汇总

| 事件 | 触发位置 | parts 表 | quotes 表 | 表结构 | 已提交旧数据 |
|---|---|---|---|---|---|
| 同编号重复（同 source_ref） | `storage.py:22,25` REPLACE | 整行被覆盖 | 整行被覆盖 | 不变 | 不变 |
| 同编号重复（source_ref 不同） | 报价复合主键 `storage.py:17` | 整行被覆盖 | **新增一行并存** | 不变 | 不变 |
| 转换异常（价格/包装量/缺键/非数组） | `normalize.py:24,27,29`、`sources.py:18` | 本轮写入全部回滚 | 同左 | 保留（DDL 已提交） | 原样保留（实测） |
| 数据库构造异常（路径不可写） | `storage.py:9` | 不存在/不可用 | 同左 | — | 不触及 |
| 数据库运行期异常（schema 不兼容等） | `storage.py:22/25` 抛 OperationalError | 本轮写入回滚 | 同左 | 保留 | 原样保留（实测） |

---

## 5. 重复记录场景（完整复现与观察）

**场景设计**：先用仓库原始 fixture 建库，再用一个外部构造的单记录 fixture（同编号 `AB1204`，但描述/包装量/价格/交期/状态全不同，`/tmp/baseline-data/dup_cross_batch.json`）分两轮导入，刻意区分 `source_ref` 字符串。

```bash
# 轮次 1：仓库 fixture（CLI 默认 source_ref = 传入的绝对路径）
python3 -I /tmp/baseline-scripts/run_pipeline.py \
  /workspace/fixtures/supplier_payload.json /tmp/baseline-data/baseline.sqlite3
# 轮次 2：外部重复记录，显式给一个相对路径形态的 source_ref
python3 -I /tmp/baseline-scripts/run_pipeline.py \
  /tmp/baseline-data/dup_cross_batch.json /tmp/baseline-data/baseline.sqlite3 \
  USD "fixtures/supplier_payload.json"
# 轮次 3：与轮次 2 完全相同的参数再跑一次
```

观察结果：

1. **轮次 1 后**：parts=1 行、quotes=1 行（fixture 内部两条记录先碰撞一次，后者覆盖前者）。
2. **轮次 2 后**：
   - parts 仍为 1 行，但内容被替换为 `('AB1204','ACME','Cross-batch replacement text','EA',2)`——目录身份只认编号，REPLACE 生效。
   - quotes 变为 **2 行**：新行 `(...,'19.99',9,'backorder','fixtures/supplier_payload.json')` 与旧行 `(...,'18.75',5,'available','/workspace/fixtures/supplier_payload.json')` 并存。原因是两个 `source_ref` 字符串（绝对路径 vs 相对路径）不同，复合主键不同，`INSERT OR REPLACE` 退化为 INSERT。**即：同一文件用不同路径调用，会被系统当成两个报价来源。**
3. **轮次 3 后**：parts=1、quotes=2，计数不再变化——三元主键完全相同时恢复 REPLACE 幂等。
4. 对照实验：对同一库重复跑轮次 1 的原始命令，两表计数始终 1/1，内容稳定（REPLACE 幂等性确认）。

基线含义：系统没有任何"重复检测/告警/合并"机制；`processed` 计数（`pipeline.py:18`）不反映去重；报价侧的去重身份对 `source_ref` 的字面值敏感，而该值目前直接取自命令行路径（`cli.py:16`），没有规范化。

---

## 6. 失败输入场景（完整复现与观察）

**场景设计**：两条记录的 fixture，第 1 条合法、第 2 条价格为 `"not-a-number"`（`/tmp/baseline-data/fail_bad_price.json`），分别对全新空库和已有数据的库运行。

```bash
python3 -I /tmp/baseline-scripts/run_pipeline.py \
  /tmp/baseline-data/fail_bad_price.json /tmp/baseline-data/fail1.sqlite3
# 退出码 1，堆栈落点：
#   normalize.py:22  decimal.InvalidOperation
#   normalize.py:24  raise ValueError("invalid price for BAD-002") from exc
```

观察结果：

1. **异常类型与位置**：`Decimal('not-a-number')` 抛 `InvalidOperation`，被 `normalize.py:23-24` 包装为 `ValueError: invalid price for BAD-002`。这是**规范化层**异常，发生在第 2 条记录进入 `save_*` 之前；整条管道（`pipeline.py:15`）与 CLI（`cli.py` 无 try/except 业务分支）均不捕获，进程退出码 1。
2. **全新空库的最终状态**：`parts`、`quotes` 两表均为 0 行，但表结构存在——第 1 条合法记录本已在第 1 轮循环中写入同一事务（`pipeline.py:16-17` 先于第 2 条失败），因 `pipeline.py:19` 的 commit 未执行，`close()` 时随事务回滚；建表 DDL 因 `storage.py:19` 的独立 commit 而保留。
3. **对已有数据库的影响**：把含 1 条 parts、2 条 quotes 的库复制后再跑同一失败输入，失败后两表内容与失败前**逐字节一致**（实测旧行全部保留、计数 1/2 不变）。失败运行不污染已提交状态。
4. 同批次补充实测的其他失败模式，落点与状态一致（均回滚、表保留、退出码 1）：
   - `package_quantity: 0` → `ValueError: package_quantity must be positive`（`normalize.py:27`）；
   - 顶层 payload 是对象不是数组 → `ValueError: supplier payload must be a list`（`sources.py:18`），注意此时 sqlite 文件已被创建且含两张空表（构造顺序：`cli.py:14` 先建 store，`cli.py:16` 才 read）；
   - 缺 `supplier` 键 → 裸 `KeyError: 'supplier'`（`normalize.py:29`），无包装、无字段级错误信息。

**数据库异常的平行证据**（`/tmp/baseline-scripts/db_exception_experiments.py`）：

- 库路径指向不存在的目录 → `sqlite3.OperationalError: unable to open database file`，抛在 `storage.py:9` 构造期，任何记录都未被处理。
- 预置不兼容的 `parts` 表（2 列）→ 首次 `save_part` 即抛 `OperationalError: table parts has 2 columns but 5 values were supplied`（`storage.py:22`）；预置的 `('PREEXISTING-1','keep-me')` 行失败后原样保留，quotes 0 行。
- 人为让 `save_quote` 在 `save_part` 成功后失败（同连接 DROP quotes 表）→ `OperationalError: no such table: quotes`，关闭连接后 parts 计数回到 0，且被 DROP 的表随事务回滚恢复——证明 **parts/quotes 两次写入共享同一事务，部分成功不会留下半截状态**。

---

## 7. Node.js ↔ Python 交接边界（实测）

1. **唯一的数据流形态是 stdout JSON 文件约定，无进程间调用**：`portal_runner.mjs` 全文只有 `console.log(JSON.stringify(...))`（第 19、38 行），没有任何写文件、起子进程或数据库代码；Python 侧只从文件路径读取（`sources.py:13-16`）。两侧通过什么文件、什么调度衔接，仓库内没有代码表达。
2. **dry-run 不校验配置**：`--dry-run` 分支（`portal_runner.mjs:46`）绕过 `validate()`，无任何环境变量也能退出 0，输出 `{"mode":"dry-run","target":"not-configured","queryPresent":false}`——这是状态信封，不是物料数据。
3. **真实运行先校验后动态装载**：缺环境变量时 `portal_runner.mjs:15` 抛 `missing portal configuration: ...`（实测缺 1 个与缺 4 个均退出码 1）；配置齐全才在第 23 行 `await import("playwright")`。本环境未装 playwright，实测到 `ERR_MODULE_NOT_FOUND`——依赖缺失只在真实路径才暴露。
4. **真实抓取的输出契约与 Python 不兼容（关键边界证据）**：门户路径产出的字段是 camelCase 三字段 `{partNumber, stockStatus, price}`（`portal_runner.mjs:33-37`），且顶层是单对象。把两种 Node 输出直接喂给 Python 管道实测：
   - dry-run 输出 → `ValueError: supplier payload must be a list`（`sources.py:18`）；
   - 门户形状输出（包成数组后）→ `KeyError: 'supplier'`（`normalize.py:29`），因为 Python 要求 snake_case 的 `supplier/part_number/brand/...` 且 supplier 为必填。
5. 因此并入前必须新增一个**显式适配层**（数组包装 + camelCase→snake_case 字段映射 + 补齐 supplier/source_ref 等必填项 + 落盘为 Python 契约文件），该适配层在当前基线中不存在；门户侧 `stockStatus` 还可能是字面量 `"unknown"`（`portal_runner.mjs:35`），恰好落入 Python 状态别名表的兜底分支（`normalize.py:36`）。

---

## 8. 环境限制与证据可信度

- `pytest` 未安装，`tests/test_normalize.py` 无法直接执行；其唯一测试的 4 条断言（编号 `AB1204`、品牌 `ACME`、价格 `Decimal('18.50')`、目录键 `AB1204`）已由第 3 节的逐层追踪脚本用同一批源码独立复现通过。
- `playwright` 未安装且无真实门户，Node 真实抓取路径以源码审计 + 动态导入失败实测 + 输出形状静态证据（`portal_runner.mjs:33-37`）替代；dry-run 与配置校验路径为真实执行。
- 所有实跑产物位于 `/tmp/baseline-scripts/` 与 `/tmp/baseline-data/`，仓库内仅新增本文档；业务源码零改动。

## 9. 给并入改造的基线清单（冻结项，不在本次实施）

1. 同编号静默 REPLACE、报价三元主键、`source_ref` 取命令行字面路径这三条身份规则是现状行为，改造若调整需显式声明迁移。
2. 单运行事务原子性依赖"末尾唯一 commit + close 回滚"，无重试/断点；失败输入无死信、无部分成功提示，只有堆栈与退出码 1。
3. Node/Python 之间缺适配层与契约校验，当前不存在可直接复用的自动化交接。
4. `int()` 静默截断浮点包装量、未知库存状态落 `unknown`、缺键裸 KeyError、价格以 TEXT 存储等隐式转换，均属本基线已确认行为。
