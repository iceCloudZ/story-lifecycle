# Story 状态治理设计 v0.1

> **✅ P1+P2 已实现(2026-07-13)。** 改动覆盖:配置层(minimal.yaml 加结项态+tapd_state_map)、DB(is_test 列+show_test 过滤)、同步层(sync_service 映射+_is_forward 防回退)、API/CLI(remap_lifecycle 透传)、前端(看板过滤 is_test+status 终态)。测试 10 条全绿,pytest 958 passed / ruff / tsc build 全通过。
>
> **问题**:班车看板暴露了三个深层问题——存量 story 状态没地方统一管理、TAPD 状态没同步到本地状态机、测试 story 污染看板。
>
> **本文不是班车看板的补丁,而是治理这三类问题的设计依据。** 决策定下来后,follow-up 按本文实现。
>
> **状态**:v0.1,P1+P2 已落地。基于代码现状调查(`models.py` / `sync_service.py` / `tapd_source.py` / `minimal.yaml` / 前端)。
> **范围**:`packages/story-lifecycle` 为主,`story-miner`/`knowledge` 后续按需消费状态。

---

## 0. 一句话

story 表有 **三个互不连通的状态字段**(`status` 引擎态 / `lifecycle_state` 业务态 / `tapd_status` TAPD 原态),同步只搬 `tapd_status` 当标签,**没有任何归一层把它们对齐**;加上无测试数据隔离,导致班车看板把"已完成/测试"的 story 全拉进来。治理 = 加 TAPD→lifecycle 映射 + 加 `is_test` 标记 + 统一状态可见性口径。

---

## 1. 现状(调查事实,以代码为准)

### 1.1 三个状态字段各跑各的

| 字段 | 谁写 | 取值 | TAPD 同步? |
|---|---|---|---|
| `status`(引擎态) | driver | active/paused/blocked/completed/failed/aborted/archived/idle | ❌ 不同步(同步只给新 story 默认 `idle`,见 `models.py:1119`) |
| `lifecycle_state`(业务态) | 人点推进(`advance_lifecycle_state`,api.py:833) | 开发/测试/上线 | ❌ **完全不同步** |
| `tapd_status`(TAPD 原态) | TAPD 同步(`upsert_story_from_source`) | 见 §1.2 | ✅ 同步,但**只当标签用,不驱动任何状态机** |
| `intake_state`(接入态) | intake 流程 | candidate/ready | 半同步(仅新 story 初始 `candidate`,更新不覆盖,`models.py:1101-1102`) |

**关键**:同步链路 `sync_service.py:46-92` 更新已有 story 时,update 字典里**只有** title/deadline/priority/owner/tapd_status/tapd_url/tapd_type/parent_key。`lifecycle_state` 和 `status` **根本不在同步路径上**。

### 1.2 TAPD 状态值(来自 `tapd_source.py:20-25`)

**需求(story)开态**:`open` / `progressing` / `reopened`(`tapd_source.py:20-22` fetch 默认过滤)
**缺陷(bug)开态**:`new` / `reopened` / `assigned` / `resolving`(`tapd_source.py:23-25`)
**终态(共用)**:`resolved` / `rejected` / `closed`(`models.py:552` `COMPLETED_STATES`)

⚠️ **需求与 bug 的开态不同,终态共用。** 映射表必须分支处理。

### 1.3 现有的"映射"只有一个,且只过滤不改状态

`models.py:552` + `list_visible_stories:589-590`:
```python
COMPLETED_STATES = frozenset({"resolved", "rejected", "closed"})
...
if not show_completed:
    stories = [s for s in stories if s.get("tapd_status") not in COMPLETED_STATES]
```
这是 `tapd_status` 影响"可见性"的**唯一**地方,只过滤、不改 `status`/`lifecycle_state`。grep 全 `src/` 无任何 `tapd_status → lifecycle_state` 转换函数(零命中)。

唯一的反向映射是**本地→TAPD 写回**(`tapd_source.py:130-135`):
```python
TAPD_STATUS_MAP = {"completed": "done", "blocked": "reopen", "aborted": "postponed"}
```
这是写回 TAPD 的,与读入无关。

### 1.4 lifecycle_state 是配置驱动,且"结项"不存在

定义在 `minimal.yaml:75-87`(唯一来源):
```yaml
story_states:
  开发: {stages: [design, build], next: 测试, ...}
  测试: {stages: [verify], next: 上线, ...}
  上线: {stages: [], next: null, confirm: {type: none}}   # ← 终态是"上线",不是"结项"
```

⚠️ **"结项"是前端硬编码的幽灵状态**(`ReleaseTrainBoard.tsx:8` `['开发','测试','上线','结项']`):
- 配置里没有"结项"(终态叫"上线")
- `advance_lifecycle_state`(api.py:833)docstring 写"开发→测试→上线"
- → 看板第 4 列"结项"**永远不会有 story 落进去**,是死列
- `models.py:266` 注释也写了"结项"(文档惯性,代码未落地)

### 1.5 测试数据无任何隔离

- story 表**没有** `is_test`/`is_demo`/`category` 字段(grep 零命中)
- 测试 fixture 用临时 DB(`conftest.py` `_isolated_db` 重定向到 `tmp_path`,**不污染真实库**)—— 这保护了 CI,但**本地手跑测试 / demo / seed 可能写到真实库**
- 测试 story_key **无统一前缀**:既有 `TEST-`/`S-`/`REQ-`/`BUG-`,也有伪装成真实的 `tapd-1001`(与真实 sync 出的 `tapd-{source_id}` 格式**完全无法区分**)
- seed 脚本 `seed_quality.py` 不造 story(只造 finding/pattern);`demo.py` 用 `demo-hello` key

### 1.6 同步纯手动,无定时

`sync_service.py` 无 cron/loop。触发入口:
- CLI `story sync --workspace <abs> [--status-only] [--all] [--id <tapd_id>]`(`sync_cmd.py:24`)
- API `POST /api/sync/tapd`(`api.py:1758`)
- `--status-only` 模式只刷新已有 story 的 TAPD 状态、不新建(`sync_service.py:66-67`)—— **适合做映射刷新入口**

---

## 2. 三个问题的根因(同一棵树)

```
班车看板"已完成的也拉进来了"
   ├─ 根因 A:GET /api/story 默认返回 status='completed'(list_visible_stories:579)
   │          前端只按 intake_state 过滤,没按 status 过滤
   ├─ 根因 B:lifecycle_state 与 TAPD 完全解耦 → 存量状态全靠人点,没人点就停在默认'开发'
   │          → 看板上"已完成"的 story 还挂在开发/测试列(因为 lifecycle_state 没推进)
   └─ 根因 C:无测试数据隔离 → 本地跑测试/seed 造的 story 混进真实库 → 看板显示

存量状态没地方管理
   ├─ 根因 B(同上):无 TAPD→lifecycle 映射 → 存量没法批量校准
   └─ 缺批量管理入口(只有单条 /lifecycle/advance,且只能往前推一格)

测试 story
   └─ 根因 C(同上):无 is_test 字段 + key 无约定
```

---

## 3. 决策(已定)

| # | 决策 | 选定方案 | 理由 |
|---|---|---|---|
| D1 | TAPD→lifecycle 归一 | **同步时按映射表自动写 lifecycle_state**(增量)+ 一次性回填脚本(存量) | 一次配好,增量存量都自动对齐;复用现有 `--status-only` 同步入口 |
| D2 | 测试数据隔离 | **story 表加 `is_test INTEGER DEFAULT 0` 列**,测试代码造数据时置 1,看板/列表默认过滤 | 比命名约定可靠;迁移成本低(一个 ALTER) |
| D3 | 状态可见性口径 | 看板额外按 `status` 过滤(藏 completed/failed/aborted/archived)+ 按 `is_test` 过滤 | 双保险,不依赖单一字段 |

---

## 4. 设计:D1 — TAPD → lifecycle_state 映射

### 4.1 映射表(配置驱动,放 profile)

在 `minimal.yaml`(及有 story_states 的 profile)新增 `tapd_state_map` 段。**需求与 bug 分支**:

```yaml
# TAPD 状态 → 本地 lifecycle_state 映射(同步时自动写 lifecycle_state)。
# 仅当映射结果的 lifecycle_state 是"前进"(相对当前值)才写,防回退。
# 未命中的 tapd_status 不动 lifecycle_state。
tapd_state_map:
  story:                          # tapd_type=story
    progressing: 开发             # 进行中 → 开发(或保持)
    resolved: 上线                # 已解决 → 上线(等车)
    rejected: 上线                # 被拒也算走到上线终态(按业务定)
    closed: 上线                  # 关闭 → 上线(终态)
  bug:                            # tapd_type=bug
    assigned: 开发
    resolving: 测试               # 修复中 → 测试
    resolved: 上线
    closed: 上线
  # open / new / reopened 等"未启动"态不映射(保持当前 lifecycle_state)
```

> **这张表已定稿(见 §6 决策)。** 上方为说明版本;实际落地的最终表见 §6。map key 用 `tapd_type`(story/bug/subtask),不是 `item_type`(requirement/bug)—— 因 sync_service 先派生 tapd_type 再映射,且 tapd_type 是存储字段。

### 4.2 映射执行点:`sync_service.py` 增量

在 `sync_service.py` 更新已有 story 的分支(line 46-61 附近)和新建分支(line 77-92)各加一段:

```python
# 伪代码
mapped = tapd_state_map.get(item.item_type, {}).get(item.status)
if mapped and _is_forward(current_lifecycle, mapped):
    updates["lifecycle_state"] = mapped
```

`_is_forward` 按 profile 的 story_states 链(开发→测试→上线)判断 `mapped` 是否在 `current` 之后,防回退(避免 TAPD reopen 把已推进的状态拉回)。

**幂等**:重复同步同状态不会反复写(值相同);防回退保证不会因 TAPD 状态抖动倒退。

### 4.3 存量回填:`story sync --status-only` 复用

存量 story 已经有 `tapd_status`(历史同步过),只是没映射到 lifecycle_state。加一个一次性回填:

```bash
story sync --status-only --remap-lifecycle --workspace <abs> --all
```

`--remap-lifecycle` 新增 flag(`sync_cmd.py` + `sync_service.py`):对每个已有 story,按 §4.1 表重算 lifecycle_state(同样防回退)。这是个一次性治理命令,跑完存量就对齐了。

### 4.4 不破坏的不变量

- `intake_state` 仍不同步覆盖(`models.py:1101-1102` 不变)
- `release_train` 仍不同步覆盖(班车看板铁律)
- 映射只写 `lifecycle_state`,不写 `status`(引擎态仍由 driver 管)

---

## 5. 设计:D2 — `is_test` 标记字段

### 5.1 Schema(`models.py`)

幂等迁移,同 `release_train` 范式:

```python
# story 测试数据标记:1=测试/demo 数据,看板与列表默认过滤。
try:
    conn.execute("ALTER TABLE story ADD COLUMN is_test INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
```

加进 `VALID_COLUMNS`(`models.py:37` 附近)。DEFAULT 0 保证存量真实数据不受影响。

### 5.2 写入点(测试代码造数据时置 1)

- **测试 fixture**:`conftest.py` 的 `create_story`/`upsert_story` helper 默认 `is_test=1`(测试库本就隔离,但加上双保险,且统一语义)
- **demo/seed 脚本**:`demo.py` 造的 `demo-hello` 置 `is_test=1`
- **真实 sync**:`upsert_story_from_source` 不显式传 → 取 DEFAULT 0(真实数据)

### 5.3 过滤点(D3 口径)

- **班车看板**:`ReleaseTrainBoard.tsx:43` boardStories 加 `&& !s.isTest`
- **StoryList / API list**:`list_visible_stories` 默认过滤 `is_test=0`,加 `show_test` 参数才显示(对标现有 `show_all`/`show_completed`)
- **Story 类型**(`client.ts`):加 `isTest?: boolean`

---

## 6. 决策(已定,2026-07-13)

原三个待定问题已拍板,实现按此执行:

### Q1. 要不要"结项"状态? → **要,TAPD closed → 结项**

- 配置加第 4 态"结项":`上线.next: 结项`,`结项: {stages: [], next: null}`(真终态)
- 触发:TAPD `closed` 直接映射到"结项"(跳过"上线"的等车语义)
- `resolved` → "上线"(已解决待发布),`closed` → "结项"(真终态/归档)
- 前端看板第 4 列"结项"不再是死列

### Q2. bug 的 TAPD 状态映射? → **resolving → 开发**

bug 流转:assigned → 开发,resolving → 开发(修复中仍算开发),resolved → 上线,closed → 结项

### Q3. 映射"防回退"? → **纯防回退,不允许回退**

`_is_forward` 沿 next 链判断;TAPD reopen 不把 lifecycle_state 拉回。无"允许特定回退对"的需求。

### 最终映射表(已落地 minimal.yaml)

```yaml
tapd_state_map:
  story:
    progressing: 开发
    resolved: 上线        # 已解决 → 上线(等车/待发布)
    closed: 结项          # closed → 结项(真终态)
    rejected: 结项
  bug:
    assigned: 开发
    resolving: 开发       # Q2 拍板
    resolved: 上线
    closed: 结项
  subtask:
    progressing: 开发
    resolved: 上线
    closed: 结项
```
(open/new/reopened 等未启动态不映射)

---

## 7. 实现分阶(给 follow-up)

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **P1**(最小可合并) | D3 看板过滤:`status` 非终态 + `is_test=0`(前端改 + `is_test` 字段迁移) | 无 |
| **P2** | D1 映射:profile 加 `tapd_state_map` + `sync_service` 增量映射 + `--remap-lifecycle` 回填 | Q1/Q2 拍板 |
| **P3** | D2 完整落地:测试 fixture / demo 置 `is_test=1` + list API `show_test` 参数 | P1 |
| **P4**(可选) | 状态管理 UI:批量改 lifecycle_state 的入口(补"存量管理没地方"的洞) | P2 |

P1 不依赖 Q1/Q2,可以先做(立即解决看板污染);P2 需要业务确认映射表。

---

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 映射表配错 → 批量把存量 lifecycle_state 写错 | 回填命令先 `--dry-run`(复用现有 dry_run);映射防回退 |
| TAPD 状态语义与业务实际不符 | Q1/Q2 必须业务确认后再做 P2;P1 不碰映射,零风险 |
| `is_test` 漏标 → 测试数据仍进看板 | 双保险:字段 + 看板显式过滤;存量用一次清理脚本扫可疑 key |
| 加列迁移影响老库 | 幂等 ALTER + DEFAULT 0,同 release_train 范式,已验证 |

---

## 9. 与现有文档的关系

- **本文**(根 `docs/`):monorepo 级状态治理,跨包(story-miner 未来也消费 lifecycle_state)
- `packages/story-lifecycle/docs/design-dual-track-kanban.md`:班车看板设计,只管 release_train 一个字段
- `packages/story-lifecycle/docs/review-release-train-board.md`:看板 review,本文解决其中 S4/S5 的深层根因
- 本次治理**不改** kanban 设计文档的 release_train 模型,只补状态归一与隔离层
