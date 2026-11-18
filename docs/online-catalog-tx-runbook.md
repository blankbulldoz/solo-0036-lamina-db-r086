# 在线目录变更与活动事务交错操作手册

适用对象：LaminaDB 维护者（在线建立关系或索引的窗口操作）。
适用版本：基线 `solo-0036-lamina-db`（日志格式版本 1）。本手册只描述当前已实现能力，不新增在线索引、模式演进或事务特性；所有结论均可由 `docs/interleave/` 下的真实交错实验复现。

---

## 1. 先建立正确的心智模型：两份"目录事实"

维护窗口里最容易犯的错误，是用 `GET /catalog` 当前列表去推断一个旧事务"能看见什么、能不能提交"。这两者是不同的事实：

| 事实 | 持有者 | 内容在何时冻结 | 观察入口 |
| --- | --- | --- | --- |
| **生效目录** | 数据库头部序号 `sequence` | 每次 DDL/提交推进，全局可见 | `GET /catalog`、`GET /stats` |
| **事务目录视图** | 每个事务的 `snapshot` 序号 | `POST /transactions` 成功的一瞬间 | `GET /stats` 的 `active[].snapshot` |

源码锚点：事务在开始时记录 `Snapshot: d.sequence`（`internal/engine/transaction.go:42`）；关系和索引分别带 `Created` 序号（`internal/engine/constraints.go:49`、`internal/engine/constraints.go:69`）。因此：

- 一个事务**只能使用 `Created <= 自己 snapshot` 的关系和索引**，与当前目录里已经有什么无关。
- 唯一索引的强制点在**提交时**，且面向"提交后最终可见状态"，不是面向事务开始时的目录（`internal/engine/constraints.go:10-21`、`internal/engine/transaction.go:175`）。
- 全局数据库锁把校验、fsync、发布串行化（`internal/engine/database.go:99-113`）。DDL 不会与提交真正并发穿透，但会与**跨越 DDL 仍然存活的长事务**形成真实交错。

> 本手册中的"旧事务"= `snapshot` 小于本次 DDL 记录序号的活动事务；"新事务"= 在 DDL 返回 201 之后才开始的事务。

---

## 2. 三个生效位置，以及如何观察它们

### 2.1 DDL 的生效位置 = 它占用的提交序号

`POST /relations` 和 `POST /indexes` 本身就是耐久记录，成功返回即占用一个连续序号，记录里 `created` 等于该序号：

```jsonc
POST /v1/lamina/indexes
{"name":"by_amplitude","relation":"spectra","column":"amplitude","unique":false}
// 201 -> {"name":"by_amplitude", ..., "created": 3}
```

观察手段（按可靠度排序）：

1. `GET /stats`：看 `sequence`（头部序号）、`active[].snapshot`（每个活事务的冻结位置）、`index_entries`（候选树规模）。
2. `GET /catalog`：看关系/索引定义及每个索引的 `created`。
3. `GET /changes?after=<seq>`：看到 `kind` 为 `relation` / `index` / `commit` / `vacuum` 的完整记录与序号，是变更消费者对齐的权威顺序。
4. 查询结果里的 `plan.snapshot`：该次查询实际使用的水位；`plan.access` 为 `index` 还是 `scan`、`plan.index` 是哪条索引，证明该水位下索引是否参与。

### 2.2 查询的生效位置 = 选定的读水位

`internal/engine/read.go:18-37` 决定水位，互斥三选一：

- 带 `transaction`：水位 = 该事务的 `snapshot`，并叠加它的私有写集；
- 带 `at`：水位 = 指定序号，必须落在保留区间 `horizon..sequence`，且关系在该序号已存在；
- 都不带：水位 = 当前 `sequence`。

规划器只选 `Created <= 水位` 的索引（`internal/query/plan.go:22-24`）。**同一条查询语句，在旧事务、历史 `at`、当前读下可能分别走 scan 和 index**——这不是不一致，而是目录快照的预期表现。实验 A 的真实结果（种子提交在序号 2，索引建在序号 3）：

| 读上下文 | 水位 | `plan.access` | `plan.snapshot` |
| --- | --- | --- | --- |
| 旧读写事务 T_old（snapshot=2） | 2 | `scan` | 2 |
| 旧只读事务 T_ro（snapshot=2） | 2 | `scan` | 2 |
| 当前普通读 | 3 | `index`（by_amplitude） | 3 |
| `at=2` 历史查询 | 2 | `scan` | 2 |

SQL 入口（`POST /sql` 带 `transaction`）复用同一条 `Query` 路径，实验中它的计划与结构化查询完全相同（同为 `scan`、snapshot=2）。

另外两条查询侧规则，维护时必须知道：

- **事务一旦对该关系有私有写入，其查询强制 scan**（`internal/engine/read.go:41-43`），即使索引在它的快照里已存在。这是为了把私有写集纳入结果，不代表索引失效。
- 索引只产生候选，真实行始终按读水位的版本链重取（`internal/engine/read.go:46-54`）。`plan.candidates`、`scanned`、`matched` 含义不同，历史候选超集下数字可能差异很大，不能用候选数当结果数。

### 2.3 唯一约束的生效位置 = 提交那一刻的最终状态

`Commit` 对写集中每个关系，取**当前头部**全表扫描再叠加本事务写集，逐条校验现存唯一索引（`internal/engine/constraints.go:10-21`）。这带来一个反直觉但确定的结论：

> 唯一索引可以在一个事务开始之后才建立，并且照样能否决这个旧事务的提交。

构建索引时只检查**当时已提交的数据**（`internal/engine/constraints.go:64-68`），看不到任何事务的私有写集。所以"索引构建成功"与"旧事务能提交成功"是两件独立的事。

---

## 3. 真实交错场景（均有实验复现，编号对应 `interleave_lab.py`）

下列每个场景都在独立临时实例上用公开 HTTP 接口真实执行，完整请求/响应见
`docs/interleave/observed-interleavings.json`。

### 场景 A — 索引建在事务存活期：旧事务看不见索引，但能继续改旧关系

交错（序号 1=关系，2=种子提交，3=索引）：

1. T_old、T_ro 在序号 2 开始；
2. 序号 3 建立非唯一索引 `by_amplitude`；
3. 两个旧事务的同条件查询仍走 `scan`（见表 2.2），当前读走 `index`；
4. T_old 对**旧关系** `spectra` 的 `put` 仍返回 `staged`，随后提交成功（序号 4）。

边界：旧事务看不见的是"新索引和新关系"，**不是旧关系的写权限**。只要关系在它的快照里已存在，写入、删除、提交路径都不受后来索引影响（唯一约束除外，见 B）。

### 场景 B — 后建的唯一索引，让构建成功之后的旧事务提交失败

交错：

1. T_dup 暂存两行 `band="shared"`（sample=4、5）；T_ok 暂存一行不冲突数据；
2. 对 `band` 建唯一索引 `by_band_uq`：已提交数据无重复，**构建成功**（created=3）；
3. T_dup 提交 → `409 {"code":"conflict","message":"unique constraint by_band_uq violated"}`，事务闭合；
4. 对同一 id 再提交 → `404 transaction not active`（闭合事务不可复用）；
5. T_ok 提交 → 成功（序号 4）；查询确认 T_dup 的私有行零泄漏，`stats.active` 为空。

维护含义：**不能向调用方承诺"索引建立时没报错，你们手上的编辑就都能提交"。** 构建期检查覆盖不到私有写集，提交期才是最终闸门。失败是整笔回滚：该事务的全部私有写入都不发布，也不消耗序号。

### 场景 C — 对照：索引先建时，并发新事务抢同一唯一值

同样的重复值，顺序反过来：

1. 先建唯一索引，再开 T_a、T_b，各自暂存 `band="shared"`；
2. T_a 先提交成功；T_b 提交 → 409 唯一冲突并闭合；
3. T_swap 在一个事务内把 sample=1→b2、sample=2→b1 做**值互换**，提交成功——唯一校验针对最终状态，事务内临时互换允许；
4. T_coll 把 sample=3 改成现存的 b1 → 409。

B 与 C 的错误码、错误消息完全一致，区别只在事务开始时索引是否已存在。**调用方无法也不应依赖"我开始时有没有这条索引"来预判成败，必须处理提交期 409。**

### 场景 D — 同键写冲突与索引无关，且附带写入一并回滚

1. T_x、T_y 在无索引时开始，都改 sample=1，T_y 还附带写 sample=7；
2. 交错中建立一条**非唯一**索引；
3. T_x 先提交成功；T_y 提交 → `409 write conflict on spectra`；
4. 当前读数：sample=1 是 T_x 的值，sample=7 不存在。

first-committer-wins 判定看的是该主键版本链是否在事务快照后又出现新版本（`internal/engine/transaction.go:169-172`），与有没有索引、索引是否唯一无关。输家事务里其他键的写入同样不发布。

### 场景 E — 后建关系对旧事务完全不可见；重启不复活事务

1. T_old（snapshot=2）存活期建立新关系 `readings`（created=3）；
2. T_old 写 `readings` → `409 relation outside transaction catalog snapshot`；查它 → `410 relation did not exist at snapshot`；
3. 新事务 T_new（snapshot=3）正常写并提交；`at=2` 查新关系 → 410；
4. 重启后：`stats.active=[]`，T_old 的 id 再提交 → 404，其未提交的 sample=4 不存在；目录里 `readings` 的 `created=3` 保持不变。

事务是纯内存对象，**不写日志、不跨重启**（重启时事务表直接清空，见 `internal/engine/database.go:96` 与 `Open` 重建路径）。重启只恢复已耐久的 DDL 和提交。

### 场景 F — 长读者钉住回收水位；重启后唯一约束继续生效

1. T_ro（snapshot=2）存活，T_w 把 sample=1 改成 999 并提交（序号 3），序号 4 建唯一索引；
2. `vacuum horizon=3` 被钉回到 2：返回 `pinned=true, horizon=2, removed=0`；T_ro 仍读到旧值 10；
3. 关闭 T_ro 后再次 vacuum → `horizon=3, removed=1, pinned=false`；此后 `at=2` 查询 → `410 snapshot unavailable; retained interval 3..6`；
4. checkpoint 后重启：唯一索引仍在目录，新事务提交冲突值仍被 409 拒绝；`commits_since_open=0` 但数据行数不变。

### 场景 G — 失败的 DDL 不留痕、不推进序号

1. 已提交数据里两行 `band="dup"`，对其建唯一索引 → 409；
2. 构建前后 `stats.sequence` 都是 3，目录中无该索引（无部分发布）；
3. 重复索引名 → `400 index already exists`；未知列 → `400 unknown index column`；重复关系名 → `409 relation exists`。

失败发生在耐久发布之前（`internal/engine/constraints.go:61-72`），因此序号不被消耗，调用方修正定义后可用**同名重试**。

---

## 4. 维护窗口操作顺序（短窗口版）

不要排空全部事务（会丢长编辑），也不要无条件保留全部事务（唯一索引上线后仍会有调用方在提交时失败）。按下列顺序把不确定性变成可观测、可预案的清单。

### 步骤 0：冻结窗口前的基线采集

```sh
curl -s 127.0.0.1:7386/v1/lamina/stats  > stats-before.json
curl -s 127.0.0.1:7386/v1/lamina/catalog > catalog-before.json
```

记录 `sequence`、`horizon`、每个 `active[].snapshot` 和事务 id、`index_entries`。
`snapshot` 明显落后头部的事务就是本窗口的"旧事务"，也是 vacuum 会被钉住的来源。

### 步骤 1：建立唯一索引前，先对已提交数据做冲突预检

唯一索引构建只校验已提交数据。预检就是一次试构建：失败返回 409 且不留痕（场景 G），不会污染目录、不耗序号。若失败，先在已提交层面消除重复，再安排窗口；**不要**在重复未消除时把窗口时间花在反复构建上。

非唯一索引没有这一步，它不会让任何提交因约束失败。

### 步骤 2：提前识别"上线后仍可能提交失败"的活动事务

对每个活动事务（`stats.active`），无法从外部直接读取它的私有写集内容，因此按两类预案通知属主：

- **唯一索引窗口**：所有 `snapshot < 索引 created` 且仍可能写目标列的事务，都要被告知"提交可能返回 409，需要在新事务里重试"。这是窗口内唯一无法提前消除的失败来源（场景 B）。
- **写冲突**：任何与他人改同一主键的事务，本来就可能 409（场景 D），与本次 DDL 无关，但窗口内应一并监控。

### 步骤 3：执行 DDL，并立即核对生效位置

```sh
curl -s .../indexes -d '{...}'            # 记录返回的 created 序号
curl -s .../stats                          # 确认 sequence 推进、index_entries 出现
curl -s ".../changes?after=<before-seq>"   # 确认 kind=index 记录落盘且序号连续
```

期望：返回 201；`stats.sequence` 增加 1；`catalog.indexes` 出现该索引；changes 中能看到对应 `index` 记录。
出现 409（唯一冲突）→ 目录未变、序号未变，按步骤 1 处理；出现 400（命名/列错误）→ 修正请求体后同名重试。

### 步骤 4：处理窗口内的提交失败，而不是阻止它们发生

- 对 409 响应：让调用方**开新事务重放编辑**。原事务已闭合，复用同一 id 只会再得 404。重试前先重新查询当前值（唯一冲突可能来自他人已提交的行）。
- 不要尝试"先回滚再补救"——失败提交本身已经完成整笔回滚，私有写集已丢弃，没有残留需要清理（场景 B 的 `stats.active=[]` 可核对）。
- 对 503（journal poisoned / database closed）：这是耐久结果不确定，实例已拒绝继续服务。**不要重试提交**（基线无请求幂等），按第 6 节重启核对实际落盘状态后再决定补偿。

### 步骤 5：窗口收尾的回收与检查点

- 只要还有活动事务，`vacuum` 的实际水位会被钉到最低活动快照（场景 F）。想推进回收，先让属主关闭长事务/长只读会话，再发 vacuum；用返回的 `pinned` 和 `horizon` 确认，而不是假设请求水位生效。
- 窗口结束前发一次 `POST /checkpoint`，把目录、保留版本和水位固化进镜像，缩短下次重启的重放长度。

---

## 5. 影响预判速查表

| 你观察到的情况 | 旧事务查询 | 旧事务写旧关系 | 旧事务提交 | 新事务 |
| --- | --- | --- | --- | --- |
| 后建**非唯一**索引 | 走 scan，结果不变（A） | 不受影响（A） | 不受索引影响；仍可能因同键写冲突 409（D） | 可使用新索引 |
| 后建**唯一**索引，私有写集无冲突 | 走 scan，结果不变 | 暂存照常 | 成功（B 中 T_ok） | 提交期受唯一约束（C） |
| 后建**唯一**索引，私有写集有冲突 | 走 scan，自己的暂存值仍可读 | 暂存照常（构建期看不见） | **409，整笔回滚并闭合**（B 中 T_dup） | — |
| 后建**关系** | 查它 → 410（E） | 写它 → 409（E） | 不涉及 | 正常使用 |
| 同主键被他人先提交 | — | — | 409 写冲突，附带写入全丢（D） | 同规则 |
| vacuum 水位高于活动快照 | 旧快照读保留 | — | — | 实际水位被钉回（F） |
| 进程重启 | 历史读按保留区间可用 | 事务消失 | 再用旧 id → 404（E） | 一切照常，约束仍在（F） |

---

## 6. 重启后的核对清单

重启只重放完整日志帧、重建索引并重新校验唯一约束（`internal/engine/database.go:57-78`）。活动事务不恢复。按以下顺序核对"确认的数据符合真正生效的目录"：

1. **进程起来且日志完整**：不完整尾帧会被自动截断并体现在 `stats.recovered_tail_bytes`；完整帧 CRC 错误会拒绝启动（fail closed），此时不要换目录绕过，先保留现场排查。
2. **目录与生效序号**：`GET /catalog` 比对窗口内建立的关系/索引及其 `created`；`GET /stats` 比对 `sequence` 与重启前最后一次 checkpoint/changes 的头部。
3. **约束仍然有效**：对每条唯一索引，用一次预期冲突的试探性事务提交（或直接对已知重复值）确认仍返回 409（场景 F 已验证重启后约束继续强制）。
4. **不确定提交的善后**：对窗口内收到 503 或网络中断的提交，用 `GET /changes?after=<窗口前序号>` 查看该事务 id 是否存在对应 `commit` 记录——存在即已落盘，不存在即未发布，再决定补偿，避免重复写入。
5. **保留区间**：`stats.horizon` 应与最后一次 vacuum 一致；低于它的 `at` 查询预期 410，这是正常语义而非故障。
6. **计数口径**：`commits_since_open` / `conflicts_since_open` / `queries_since_open` 重启归零，而 `keys`、`versions`、`index_entries` 是持久状态，不要用归零误判数据丢失。

---

## 7. 明确的适用边界（不承诺的事项）

- 不承诺所有在途编辑无损继续：后建唯一索引可以否决旧事务提交（场景 B），这是当前设计，不是缺陷。
- 不承诺串行化、谓词锁或写偏差防护；隔离级别是快照隔离 + 同键 first-committer-wins。
- 没有事务自动过期：长事务会一直钉住 vacuum 水位并占用活动事务名额（上限 128），需要显式 rollback/commit 关闭。
- 没有请求幂等：503/超时后的提交只能靠重启后 changes 核对，不能安全重发同一写请求。
- DDL 不能在用户事务内暂存；关系模式不可变更（无模式演进能力），本手册只覆盖"新增关系/新增单列索引"。
- 含私有写入的事务查询强制 scan、历史查询不使用晚于其水位的索引，都是计划层面的预期差异，结果行仍以版本链真实可见性为准。

---

## 8. 复现实验

```sh
# 构建（仓库自带 bin/laminad 是 macOS 二进制，Linux 上需自行构建）
go build -o /tmp/lamina-build/laminad ./cmd/laminad

# 运行全部 7 个真实交错场景，输出每步 HTTP 状态与响应
python3 -I docs/interleave/interleave_lab.py \
  --binary /tmp/lamina-build/laminad \
  --out docs/interleave/observed-interleavings.json
```

脚本与产品代码分离，仅使用标准库，每个场景使用独立临时数据目录并在结束时清理进程与目录。
场景与本手册章节对应：A→§2.2/§3A，B→§3B，C→§3C，D→§3D，E→§3E/§6，F→§3F/§6，G→§3G/§4步骤1。
