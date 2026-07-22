# 四 Tab 与 lifecycle_state 对齐梳理

> 状态：**进行中**。本文记录 UI 重构(commit `120605b2`)后,梳理"四个主 tab 如何反映 story 固有状态"的多轮确认过程。
> 每轮新增事实追加到末尾对应章节,不在历史结论上回改(保留推理痕迹)。

## 背景

UI 重构后,主导航收敛为四个生命周期 tab:

```
待启动 / 开发中 / 测试·上线 / 已结项
```

梳理"一个 story 如何在这四个 tab 间流转"时,发现当前实现存在分层混淆,本梳理的目标是把四个 tab 绑定到 story 的**固有业务状态**上。

---

## 第 1 轮:四个 tab 当前靠什么字段过滤?(事实)

**结论(代码核对)**:当前四个 tab 的过滤条件**不是单一固有字段**,而是三个引擎字段临时拼接:

| Tab | 当前过滤条件 | 来源文件 |
|---|---|---|
| 待启动 | `intakeState === 'ready'` | `Dashboard.tsx:20` |
| 开发中 | `lifecycleState === '开发' && status !== 'idle'` | `DevPage.tsx:13` |
| 测试·上线 | `lifecycleState === '测试' \|\| '上线'` | `TestReleasePage.tsx:14` |
| 已结项 | `lifecycleState === '结项' \|\| status === 'archived'` | `DonePage.tsx:12` |

三个字段的真实语义:

- **`status`**(引擎执行状态):`idle / planning / active / paused / blocked / completed / failed / aborted / archived`。服务于"要不要 start_story_async / resume / kill"等引擎决策。
- **`lifecycle_state`**(业务状态,但只覆盖三态):`开发 / 测试 / 上线 / 结项`。DB 默认 `'开发'`(models.py:286),**没有"待启动"初值**。
- **`intake_state`**(intake 流程标记):`candidate / ready`。`/start` promote 到 ready 后**全代码库再无写入**(核对:仅 sync_service 设 candidate、/start 设 ready)。

### 重叠问题(已确认的 bug)

`/start` 之后 `intake_state='ready'` 且永不变化,而「待启动」过滤 `intakeState==='ready'` → **一个 planning/active 的 story 同时满足「待启动」和「开发中」两个 tab 的条件,会重复出现**。

根因:没有独立的"story 业务状态"字段,靠三个引擎字段拼,字段间无互斥保证。

---

## 第 2 轮:分层澄清(共识)

**共识**:四个 tab 是 **story 的固有业务状态**,与引擎执行状态(`status`)是两个层,不能混用。

```
业务层(story 固有状态,四态互斥)     引擎层(执行状态,引擎内部)
─────────────────────────────       ─────────────────────────
待启动                               idle / planning / active / paused / ...
  ↓ 用户确认动作                      引擎该怎么跑还怎么跑
开发中                               status 的抖动(paused/blocked/emergency-stop)
  ↓ 用户确认动作                      不应导致 story 在 tab 间乱跳
测试·上线
  ↓ 用户确认动作
已结项
```

**`status` 是引擎状态机**(claude 在不在跑、暂停了没、崩了没);**四态是业务状态**(用户推进到哪一步了)。把 status 拿来当 tab 判据,就是混淆的来源。

---

## 第 3 轮:用 lifecycle_state 承载四态(决策)

**决策**:扩展 `lifecycle_state`,加初值「待启动」,四个 tab 全部直接读 `lifecycle_state`,**彻底不碰 `status`**。

### 目标四态判据

```
[待启动]    lifecycle_state === '待启动'
              (start 之后、确认规划之前)
              ↓ /plan/confirm
[开发中]    lifecycle_state === '开发'
              (含 single-pass: start 后不经 planning 的)
              ↓ /lifecycle/advance (story_state_gate 确认)
[测试·上线] lifecycle_state ∈ {'测试', '上线'}
              ↓ 上线 + 验证完成
[已结项]    lifecycle_state === '结项' || status === 'archived'
```

**分界线**:
- 「待启动」↔「开发中」的分界是 **「确认规划」动作**(`/plan/confirm`),不是 start、也不是 planning 状态本身。
- single-pass profile(start 后直接 active、无 planning 阶段)直接落「开发中」——它没有规划步骤可确认。

### 不进四 tab 的(预期内)

- `candidate` / `idle`(TAPD 同步来、未 start)→ 在「更多」→ TAPD 需求页(`/tapd`)。

---

## 第 4 轮:lifecycle_state 全部写入点(事实核对)

为落地第 3 轮决策,核对 `lifecycle_state` 在代码库的所有写入点:

| 动作 | 端点/位置 | 当前写什么 | 需改成 |
|---|---|---|---|
| TAPD 同步新建 | `sync_service.py:143` | 映射值 or 不写(DB 默认开发) | **待启动** |
| 页面「新建并开始」 | `create_story` `models.py:551` | 不写(DB 默认开发) | **待启动** |
| **确认规划** | `/plan/confirm` `api.py:3569` | **只写 status=active,不碰 lifecycle_state** | **+ lifecycle_state=开发** |
| 开发完成确认 | `/lifecycle/advance` `api.py:1020` | lifecycle_state=测试 | ✓ 已对 |
| planner 初始化 | `planner.py:862` | 读 ctx/DB/默认"开发" | 默认值改「待启动」 |

**关键缺口**:`/plan/confirm`(api.py:3569-3605)只写 `status="active"`,完全没碰 `lifecycle_state`。这是"待启动→开发中"转移的核心动作点,**必须在此写 `lifecycle_state=开发`**。

### planner.py:862 的默认值链

```python
lifecycle_state = (
    ctx.get("_lifecycle_state") or story.get("lifecycle_state") or "开发"
)
```

DB 默认从 `'开发'` 改成 `'待启动'` 后,这条链的 fallback `"开发"` 也要同步改成 `"待启动"`,否则无 lifecycle_state 的 legacy story 会被误判进开发中。

---

## 已确认的补充决策(第 5 轮)

- **TAPD 同步来已 close 的需求 → 直接落「已结项」**(按 `tapd_map` 映射走,不绕「待启动」)。
  即:sync 时 `mapped_state` 照常算,close 映射到「结项」就直接写结项。
- **老数据不写 backfill 脚本,改手动逐条过**。DB 默认值迁移(`'开发'` → `'待启动'`)
  只对**新建 story** 生效;存量 `lifecycle_state='开发'` 但从未确认规划的老 story,
  **主动一条一条人工确认**该回退成「待启动」还是保留「开发」(每条的实际情况不同,
  不能一刀切回迁,也不被动等遇到再处理)。
- 「待启动」初值中文名用「待启动」三字(与 tab 名一致)。

---

## 已实施(第 6 轮)

### 后端改动(6 处,均已落地)

- [x] DB 列默认值 `models.py:286`:`DEFAULT '开发'` → `DEFAULT '待启动'`。只对新库/新行生效。
- [x] 三处硬编码 fallback 改「待启动」:`planner.py:863`、`api.py:1043`(advance_lifecycle_state)、`api.py:3441`(plan view)。
- [x] **`/plan/confirm` 写 lifecycle_state=开发** `api.py:3598`。这是"待启动→开发中"的唯一推进点。
- [x] **归档端点同步写结项** `api.py:1153`:`status="archived"` → 加 `lifecycle_state="结项"`。归档 = 业务结项,语义同步。这样「已结项」tab 无需 status 兜底。
- [x] TAPD 同步 `_is_forward` 特判「待启动」`sync_service.py:18`:从「待启动」到任何已定义状态都算前进(待启动不在 story_states 拓扑里,是规划前的前态)。保证 close→结项、progressing→开发 等映射能正常写入。
- [x] `/start` 不写 lifecycle_state(story 还在待启动,合理)— 不动,确认。

### 前端改动(3 处过滤,均已落地)

- [x] `Dashboard.tsx`:`s.intakeState === 'ready'` → `s.lifecycleState === '待启动'`
- [x] `DevPage.tsx`:`s.lifecycleState === '开发' && s.status !== 'idle'` → `s.lifecycleState === '开发'`
- [x] `DonePage.tsx`:`s.lifecycleState === '结项' || s.status === 'archived'` → `s.lifecycleState === '结项'`(归档端点已同步写结项,无需兜底)
- [x] `TestReleasePage.tsx` 已纯 lifecycleState,不动。

### 测试改动(均已落地)

- [x] `test_story_state_machine.py`:fixture 显式 `lifecycle_state="开发"`(测的是"已在开发态"的状态机,不依赖新默认值)。
- [x] `test_agent_api.py` `TestPlanConfirm`:补 `lifecycle_state == "开发"` 断言。
- [x] `test_agent_api.py` `TestArchive`(新增):归档端点写 `lifecycle_state == "结项"`。

### 验证结果

- `pytest packages/story-lifecycle/tests/`:1118 passed,5 failed(全是子进程类测试 clarify_mcp/consult_cli 的环境问题,改动前就存在,与本次无关)。
- `ruff check`:全过。

### 待办(老数据,后置)

- [ ] 老数据逐条人工确认:`lifecycle_state='开发'` 但从未确认规划的老 story,主动逐条判断该回退成「待启动」还是保留「开发」。

### 未改的视图(明确界定范围)

- TapdBoardPage(/tapd)、ReleaseTrainBoard(/release-train):独立视角,成员过滤未改。
- DiagnosticsPage:按 status(failed/blocked)是诊断正确语义,不改。
- StoryCard badge:显示引擎 status(运行中/已暂停),是执行态信息,不改。

---

## 字段写入点速查(实施后状态)

```
lifecycle_state 写入:
  models.py:286        DB DEFAULT '待启动'(新行)
  planner.py:863       fallback '待启动'(无数据兜底)
  api.py:1043          advance_lifecycle_state fallback '待启动'
  api.py:3441          plan view fallback '待启动'
  api.py:3598          /plan/confirm → '开发'(待启动→开发中推进点)
  api.py:1153          /archive → '结项'(归档同步)
  api.py:1054          /lifecycle/advance → next_state(开发→测试→上线)
  sync_service.py:144  TAPD 新建映射(mapped_state,如 close→结项)

不写 lifecycle_state(确认合理):
  api.py:3310,3380     /start 只写 status=planning(story 还在待启动)
```
