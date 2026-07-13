# 班车看板 Code Review — 交接给 follow-up AI

> **目的**:这份文档是 review 结论,供下一个 AI(或人)按清单继续修复。
> 每条都给了 `文件:行号` + 问题 + 为什么是问题 + 怎么改。做完一条勾一条。
>
> **依据**:`docs/design-dual-track-kanban.md` v0.2(根 docs/)
> **审查范围**:Phase 1(后端 models.py + api.py + 测试)+ Phase 2(前端 6 文件)
> **审查时状态**:改动**未 commit**,在 git 工作区(`git status` 可见)
> **审查日期**:2026-07-13

---

## 🔁 Re-review(2026-07-13 第二轮)— 最新状态,先读这段

**改动已 commit** 到分支 `feature/ice/release-train-board`,commit `1b91af30 feat(release-train-board): 实现班车看板 Phase 1 + Phase 2`。

### 修复情况一览

| 原编号 | 项目 | 状态 | 证据 |
|---|---|---|---|
| 🔴 **B1** | 待分配区 DnD 拖错对象 + 空池无法接收 | ✅ **FIXED** | `ReleaseTrainBoard.tsx:177, 191-206, 160-168` —— callback 签名改为 `(draggedKey) => void`;handleDrop 用 dataTransfer 的 key;`.unassigned-pool` 加了 onDrop |
| 🟡 **S1** | 空串 `""` vs `null` 未归一 | ✅ **FIXED** | `api.py:906-907` `train = train.strip() or None`;测试 `test_empty_string_treated_as_null`(test:641-651) |
| 🟡 **S2** | event_log 缺 operator/manual | ❌ **未修** | `api.py:911-916` payload 仍只有 `{"from", "to"}` |
| 🟡 **S3** | 双数据源(zustand vs react-query) | ⚠️ **延后(可接受)** | `storyStore.ts:21-22` 仍有 `releaseTrain`/`lifecycleState` 字段但无任何路径写入(dead 字段);看板统一走 react-query,功能上无分裂 bug,但 dead 字段未清理 |
| 🟡 **S4** | `intakeState==='ready'` 过滤未注释 | ❌ **未修** | `ReleaseTrainBoard.tsx:43` 未变,无注释无 toggle |
| 🟡 **S5** | `lifecycleState \|\| '开发'` 污染 WIP | ❌ **未修** | 4 处全未变(`ReleaseTrainBoard.tsx:97,106,220`;`Swimlane.tsx:103`) |
| 🟡 **S6** | 缺幂等迁移回归测试 | ❌ **未修** | `tests/` 无 `test_init_db_idempotent*` |
| 🟡 **S7** | WIP 超额拖入无即时反馈 | ❌ **未修(可延后)** | handleDrop 无 WIP 检查;S7 本就标为 nice-to-have |

### 构建验证(本轮已实跑)

| 检查 | 结果 | 说明 |
|---|---|---|
| F1 后端 pytest | ✅ 948 passed / 4 skipped / **1 failed** | ⚠️ 唯一失败 `test_clarify_mcp.py::TestRunServerStdioChain` 是**环境问题**(用了 hermes 的 venv,`ModuleNotFoundError: story_lifecycle`),**与本改动无关**。所有 `TestReleaseTrainAPI` 测试通过 |
| F2 前端 build | ✅ `tsc -b && vite build` 干净通过 | TS 类型无错 |
| F3 ruff | ✅ All checks passed | |
| F4 eslint | ✅ 看板 6 文件零 lint 错 | 仓库有 7 个 lint 错但全在 `Dashboard.tsx`/`CodeChangesTab.tsx`/`ReactDiffViewViewer.tsx`,**未被本 commit 触碰**(pre-existing) |

### 本轮结论

**Blocker B1 已修复,可合并。** 核心铁律全部守住,无回归。构建全绿(唯一 pytest 失败是环境噪声,非本 PR)。

**剩余开放的 🟡(不阻塞合并,建议后续跟进)**,按优先级:
1. **S5** —— 影响数据准确性(null lifecycleState 被算进 WIP,数字虚高),优先修。
2. **S6** —— 设计文档 Phase 1 完成标志要求"迁移幂等",补一条测试锁死。
3. **S2** —— event_log 既然记了就补 operator/manual,审计才完整。
4. **S3 / S4** —— 需人为定夺(架构抉择/意图确认),见各条说明。
5. **S7** —— nice-to-have。

下面是原始 review 的详细条目(保留以备查考)。

---

## 一句话结论(原始 review)

实现**整体忠实于设计文档 v0.2,核心铁律全部守住,不触发返工**。但有 **1 个 Blocker(待分配区拖动 bug,会静默错改数据)** 必须修才能合并;另有 7 条 🟡 建议、3 条 🟢 可选。

修完 **B1 + S1** 即可合并。S2–S7 建议跟进但不阻塞。

---

## 核心铁律核查(全 PASS ✅,不用改)

这些是设计文档的硬约束,实现都守住了,**不要在 follow-up 里改坏它们**:

| 铁律 | 依据 | 实现位置 | 状态 |
|---|---|---|---|
| lifecycle_state 只读,人不许拖 | 设计 §3 L97、§10 L335 | 所有 drag handler 只调 `setReleaseTrain`,从不写 `lifecycleState` | ✅ |
| 同步不覆盖 release_train | 设计 §9 L318 | `models.py:1083-1105` upsert 列表里**没有** release_train;有回归测试 `test_upsert_from_source_does_not_overwrite_release_train` | ✅ |
| 唯一 schema 改动是加一列 | 设计 §4.1 L117、§11 L341 | 只加了 `release_train TEXT`,无新表 | ✅ |
| 不造新 stage / 新 profile / 新状态词 | 设计 §2 L81-82、§10 | 状态词仍 `开发/测试/上线/结项` | ✅ |
| 不动 profile / story_states / stage流水 | 设计 §7 | grep 确认未触碰 | ✅ |
| 不改 Dashboard / StoryList | 设计 §7 | git status 确认未触碰 | ✅ |
| 无 Phase 3 越界(实体表/动画/拖回状态) | 设计 §2、§8 Phase 3 | 全部没做 | ✅ |

---

## 🔴 Blocker(必须改才能合并)

### [x] B1 — 待分配区 DnD 拖错对象:拖 A 到 B,清空的是 B 不是 A ✅ FIXED(1b91af30)

**文件**:`packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:169` + `:194-200`

**问题**:

`UnassignedCard` 把 `onDropToUnassigned` 绑定到**宿主卡片自己**的 story:

```tsx
// ReleaseTrainBoard.tsx:169
onDropToUnassigned={() => handleDrop(s.storyKey, null)}   // ← 绑的是宿主 s,不是被拖的卡
```

而 `UnassignedCard.handleDrop`(`:194-200`)拿到拖来的 key 后**直接丢弃**,调宿主的 callback:

```tsx
function handleDrop(e: React.DragEvent) {
  e.preventDefault()
  const key = e.dataTransfer.getData('text/plain')   // ← 拿到了被拖卡片的 key
  if (key && key !== story.storyKey) {
    onDropToUnassigned()                              // ← 但用的不是 key,清的是宿主 s
  }
}
```

**为什么是问题**:

1. 用户把一张已分班的卡片(A)拖到待分配区某张卡片(B)上,**被清空归零的是 B,A 不动**。用户直觉是"把 A 拖回待分配",实际却误改了另一张卡。
2. 这是**静默的数据错改**,且 `release_train_changed` event log 会记一条用户根本没打算做的操作。
3. 违反 AGENTS.md「每个非执行分支必须有可见反馈」精神(这里给了错误的反馈)。
4. **附带**:待分配区容器 `.unassigned-pool`(`:160`)本身**没有 drop handler**,只有 `UnassignedCard` 卡片是 drop target。所以**当待分配区为空**(显示"暂无待分配 Story")时,**根本无法把卡片拖回待分配** —— 因为没有可接收的卡片。两个问题叠加,待分配区作为 drop target 基本不可用。

**怎么改**(两条都做):

1. **修 callback 签名**。`UnassignedCard` 的 `onDropToUnassigned` 改为接收被拖的 key:
   ```tsx
   // ReleaseTrainBoard.tsx —— UnassignedCard props
   onDropToUnassigned: (draggedKey: string) => void

   // UnassignedCard.handleDrop
   function handleDrop(e: React.DragEvent) {
     e.preventDefault()
     const key = e.dataTransfer.getData('text/plain')
     if (key) onDropToUnassigned(key)   // 用拖来的 key,不再绑宿主
   }

   // :169 的绑定改为(不再捕获宿主 s):
   onDropToUnassigned={(key) => handleDrop(key, null)}
   ```

2. **给 `.unassigned-pool` 容器加 drop handler**,这样空池子也能接收卡片:
   ```tsx
   <div
     className="unassigned-pool"
     onDragOver={(e) => e.preventDefault()}
     onDrop={(e) => {
       e.preventDefault()
       const key = e.dataTransfer.getData('text/plain')
       if (key) handleDrop(key, null)
     }}
   >
   ```

3. **加测试/手测**:从某班车泳道拖一张卡到空的待分配区 → 卡片应消失原泳道、出现在待分配区;event log 应记一条 `release_train_changed {from: "v3.2", to: null}`。

---

## 🟡 建议(应该改,不阻塞合并)

### [x] S1 — 空字符串 `""` 与 `null` 语义未归一 ✅ FIXED(1b91af30)

**文件**:`packages/story-lifecycle/src/story_lifecycle/orchestrator/service/api.py:895-913`

**问题**:

设计文档全文用 `NULL` 表示待分配(§3 L140、§6.3、§10),**从未提空字符串**。但:

- 后端 `train: str | None`(`api.py:80-81`)接受 `""`,会写入 `release_train = ""`(不是 NULL)。
- 前端分组 `ReleaseTrainBoard.tsx:59` 用 `if (s.releaseTrain)` —— 这个**碰巧**把 `""` 也归到 unassigned(truthy 判断),所以前端目前不出错。
- 但任何 `WHERE release_train IS NULL` 的后端查询会把 `""` 漏掉;event log 的 `to` 字段对 `""` vs `null` 也不区分。

**这是设计文档的漏洞**(文档没规定空串语义),建议后端归一化:

```python
# api.py set_release_train 端点里
train = req.train
if isinstance(train, str):
    train = train.strip() or None   # 空串 → None
```

并补一条测试 `test_empty_string_treated_as_null`(PUT `{train: ""}` → DB 存 NULL)。

---

### [ ] S2 — event_log 缺操作者身份

**文件**:`packages/story-lifecycle/src/story_lifecycle/orchestrator/service/api.py:907-911`

**问题**:

```python
db.log_event(story_key, s.get("current_stage") or "",
             "release_train_changed", {"from": prev, "to": req.train})
```

对比 `advance_lifecycle_state`(`api.py:870-875`)payload 带 `"auto": False` 表明人工触发,这里只有 from/to,缺**操作者身份**。release_train 是纯人控字段,审计场景("谁把这条挪到 v3.3 的")查不到人。

**注意**:设计文档压根没要求记 event,实现已经超额完成。但既然记了就记全。

**怎么改**:payload 里加 operator(从 request header / session 取),或至少标 `"manual": True`。不是 blocker。

---

### [ ] S3 — 前端两套数据源分裂(zustand store vs react-query)

**文件**:
- `packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:36-40`(用 `useQuery`)
- `packages/story-lifecycle/frontend/src/store/storyStore.ts:20-22`(给 `StorySummary` 加了 `releaseTrain?` 字段)

**问题**:

看板用 react-query 直连 API + `invalidateQueries` 刷新,**完全绕过** zustand store。`storyStore.ts:21` 给 `StorySummary` 加了 `releaseTrain?` 字段,但**没有任何 action 会更新它**(`updateStory`/`setStories` 没被看板调用)。

**后果**:Dashboard / StoryList(若用 store)和班车看板对 `releaseTrain` 的视图会有短暂不一致;`updateStory` 这条通用路径也没覆盖 releaseTrain。属于"加了类型但没接通"的半成品。

**怎么改**(二选一,需在评审时定单一数据源):

- **选项 A**:看板也走 store 的 `updateStory(key, {releaseTrain})`,乐观更新后 invalidate。
- **选项 B**:从 `storyStore.ts` 撤掉这个没人写的字段(避免误导),看板继续用 react-query。

**这是设计文档没覆盖的架构抉择**,需要人为定。

---

### [x] S4 — `boardStories` 过滤 `intakeState === 'ready'`,设计未规定 ✅ 已解决(状态治理)

> **根因已由 `docs/design-story-state-governance.md` 解决(P1+P2 落地)。**
> boardStories 过滤现在含三道闸:`intakeState==='ready'`(过了 intake)+ `!isTest`(测试数据)+ status 非终态(completed/failed/aborted/archived)。详见 governance 文档 §5。

**文件**:`packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:42-44`

```tsx
return (stories || []).filter((s) => s.intakeState === 'ready')
```

**问题**:

设计 §6 / §8 Phase 2 没提 intake 过滤。这意味着非 ready 态的 story(草稿/未 intake)在**整个看板上不出现**,包括待分配区。可能是作者有意的(只看可执行的故事),但**代码做了设计没说的事**,实现意图不透明。

**怎么改**(三选一,需人为确认意图):

1. 若 desired → 在设计文档补一句,代码留注释说明。
2. 若不 desired → 去掉过滤。
3. 折中 → 加个筛选开关("显示全部 / 仅 ready")。

---

### [x] S5 — `lifecycleState` 缺省 `'开发'` 会污染结项态与 WIP 计数 ✅ 部分缓解(状态治理)

> **部分由 `docs/design-story-state-governance.md` 缓解:** 现在前端过滤掉了 status 终态的 story,减少了 lifecycleState 错位的情况。但 `lifecycleState || '开发'` 兜底本身仍未改(null 态仍归入'开发')—— 治理聚焦在"状态归一"层(TAPD→lifecycle 映射),让存量 lifecycle_state 准确,从而减少 null 态出现。前端兜底逻辑的彻底修复(渲染'未知'而非'开发')留作后续。

**文件**:
- `packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:97, 106, 214`
- `packages/story-lifecycle/frontend/src/components/Swimlane.tsx:103`

```tsx
const state = s.lifecycleState || '开发'
```

**问题**:

老库升级时 `lifecycle_state` 迁移有 `DEFAULT '开发'`(`models.py:271`),理论上不会 NULL。但前端用 `|| '开发'` 兜底,**一旦某条 story 的 lifecycle_state 真为 NULL/空,它会被算进 '开发' 列并计入 WIP** —— WIP 数字虚高。

**怎么改**:NULL 态要么不渲染、要么单独标"未知",不要静默归入 '开发'(尤其影响 WIP 计数准确性)。建议:

```tsx
const state = s.lifecycleState
if (!state) return null   // 或渲染成 "未知" 灰色卡
```

---

### [ ] S6 — 缺幂等迁移的回归测试

**文件**:`packages/story-lifecycle/src/story_lifecycle/infra/db/models.py:276-279`

**问题**:

`driver_claim` / `lifecycle_state` / `release_train` 三处迁移都是 `try ALTER / except OperationalError: pass`,但**没有一条测试在已有 release_train 列的库上再跑一次 init_db()**来锁死幂等性。设计 §8 Phase 1 完成标志第一条就是"DB migration 幂等(老库升级不出错)"。

**怎么改**:补 `test_init_db_idempotent_with_release_train`(跑两次 init_db,第二次不抛错)。

---

### [ ] S7 — WIP 超额拖入无即时反馈

**文件**:`packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:69-78`(`handleDrop`)

**问题**:

设计 §6.2 / §10 明确"只提醒不阻断",`WipBadge` 也确实只红字 + ⚠️。但 `handleDrop` 在拖入一个已超额的泳道时**没有任何即时反馈**(不弹 toast、不闪红),用户得自己抬头看 badge。

**怎么改**:拖入后若 `wipCount > limit` 给一个非阻塞提示(badge 抖一下 / toast)。非必要。

---

## 🟢 可选(可以不改)

### [ ] O1 — 无乐观更新,拖动后短暂闪烁

**文件**:`packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:73-74`

用 `invalidateQueries` + 10s 轮询,拖完到 refetch 完成间卡片会"跳回原泳道再跳过去"。可加乐观更新(`qc.setQueryData`)。非必要。

### [ ] O2 — 新建班车存 localStorage,跨设备不同步

**文件**:`packages/story-lifecycle/frontend/src/pages/ReleaseTrainBoard.tsx:12-28`

自定义班车名存 `localStorage['release_trains']`,换浏览器就没了。**完全符合设计 §6.4 L249-250**(原话就是"localStorage 或 settings 表"),P1 才升级。不是问题。

### [ ] O3 — `release_train_changed` event 命名风格

**文件**:`packages/story-lifecycle/src/story_lifecycle/orchestrator/service/api.py:910`

对比 `story_state_transition`(带 `_transition` 后缀),新事件用 `_changed`。两种风格混存。可选统一。

---

## 项目约定核查(供 follow-up 参考)

| 约定 | 状态 | 备注 |
|---|---|---|
| E1 中文内容 | ✅ | 状态值/班车名/UI 文案全中文,`localeCompare(b, 'zh-CN')` 排序 |
| E2 Architecture Review Triggers | ⚠️ 部分 | 见下方说明 |
| E3 resolver/decider/handler 分层 | ✅ | 端点只读 + handler 写,无副作用混入判断 |

**E2 细节**:
- release_train 流转**没用 enum/状态机**,是纯字符串 + NULL,所以"cross-system state as enum"规则**未触发**(设计有意为之,§4 说明字符串够用,P1 才升级表)。
- 前端 DnD 是新交互,严格说触发"workflow changes must define state×action → action"。**实现里没有显式的 state×action 决策表**,但 legend(`:124-125`) + handleDrop 的 no-op guard(`:71`)实质上定义了语义。
- **建议**:在 `ReleaseTrainBoard.tsx` 顶部补一个简短的 state×action 注释:
  ```tsx
  // state × action:
  //   drag onto swimlane(train)   → setReleaseTrain(key, train)
  //   drag onto unassigned pool   → setReleaseTrain(key, null)
  //   lifecycle_state column      → never a drop target (read-only)
  ```
  把隐式契约显式化。不算 blocker。

---

## 构建验证(合并前必须实跑)

> ⚠️ review 时处于 plan mode,以下命令未能执行。**合并前 follow-up 必须实跑确认。**

- [ ] **F1 后端测试**:`python -m pytest packages/story-lifecycle/tests -q`
  - 重点看 lifecycle_state / release_train 相关测试
- [ ] **F2 前端构建**:`npm --prefix packages/story-lifecycle/frontend run build`
  - 脚本是 `tsc -b && vite build`,确认 TS 编译过、无类型错
- [ ] **F3 后端 lint**:`ruff check packages/story-lifecycle/src`
- [ ] **F4 前端 lint**:`npm --prefix packages/story-lifecycle/frontend run lint`
  - 即 `eslint .`

---

## 设计文档本身的漏洞(建议回填 design-dual-track-kanban.md)

这些是 review 中发现的设计文档空白,实现都做了合理猜测,但文档没背书:

1. **空串 vs null 语义未定义**(对应 S1)。文档只用 NULL,没规定 `PUT {train: ""}` 行为。
2. **`PUT {train: null}` 回待分配未显式授权**。§6.4 只说 v3.2→v3.3 是 P1,没说 null 的反向操作。实现做了且测试覆盖了,但文档没背书。
3. **intake 过滤未规定**(对应 S4)。文档没说看板只显示 ready 态。
4. **event logging 未要求**(对应 S2)。实现超额做了,但 payload schema 没规范(缺 operator)。

---

## follow-up 推荐执行顺序

1. **B1**(必须)—— 待分配区 DnD bug,改 `ReleaseTrainBoard.tsx`。
2. **S1**(建议同批)—— 后端空串归一,改 `api.py` + 补测试。
3. 跑 F1–F4 构建验证。
4. 若 F 全绿 → **可合并**。
5. 之后按优先级做 S2–S7(S2、S5 影响数据准确性,优先;S3、S4 需人为定夺)。
6. O1–O3 可选。

每改完一条回来勾 `[x]`,便于追踪。
