# 班车看板(Release-Train Board) Design

> 日期:2026-07-13
> 状态:Design(v0.2,基于代码事实重写)
> 关联主线:收尾建设期 · 山②看板 UI(初版)
> 相关文档:
> - `docs/design-web-board.md`(Web Board 总架构,本设计是其上的视图层)
> - `docs/design-board-first-planning-session.md`(board-first 原则)
> - `docs/superpowers/specs/2026-06-11-story-context-and-tapd-lifecycle-design.md`(四套权威状态)

## 版本记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.1 | 2026-07-13 | 初版(误造 track/release_phase 字段 + 新 stage,作废) |
| v0.2 | 2026-07-13 | **基于代码事实重写**:复用 `lifecycle_state`(已有)+ 新增 `release_train`(班车归属)。删除所有重复造的部分。 |

---

## 1. 背景

### 1.1 现状(代码已有)

story-lifecycle **已经有一套双层状态机**,看板的"列"不需要造:

```
外层:lifecycle_state(Story 业务状态,story 固有第一公民)
  开发 → 测试 → 上线 → 结项
  └─ 引擎自动推(planner.py:1028-1112:当前 state 的 stages 全 done → 过 confirm gate → next)
  └─ API:POST /api/story/{key}/lifecycle/advance
  └─ DB 字段已存在:story.lifecycle_state TEXT DEFAULT '开发'(models.py:265-272)

内层:current_stage(引擎阶段)
  design → build → verify(由 lifecycle_state 绑定的 stages 决定)
```

**关键事实**(代码注释原话):
- `lifecycle_state` = "Story 业务状态,独立第一公民,**不从阶段派生**(区别于引擎 status)"
- 它已经在 API 暴露(`lifecycleState`,api.py:205,696)
- 前端已有类型 + advance 调用(client.ts:33,291)
- profile 用 `story_states` 配置流转(minimal.yaml:75+)

### 1.2 缺什么

**lifecycle_state 能表达"story 做到哪了",但表达不了"story 要上哪班车"。**

作者的发布现实:
- App v3.2 班车(7 月底,跟协同部门)
- App v3.3 班车(8 月底)
- 后台快线(随时发)
- 催收线班车(跟催收部门)
- ...

一个 story 处于 `lifecycle_state='上线'`,你知道它"准备上线了",但**不知道它要上哪班车**。班车是外部归组(人决定的),lifecycle_state 是内部进度(引擎推的),**两个维度正交**。

### 1.3 痛点

作者同时维护 22 个活跃 story,真实场景:
- 22 条混在一个 list,看不出"这班车要上谁""哪些是做完等车的"
- 用平铺清单管月度班车工作 → "浆糊感"
- 缺一个**按班车分组的看板视图**

## 2. 目标

### P0(初版必做)

1. **班车泳道**:看板按 `release_train` 横向分泳道(v3.2 / v3.3 / 后台快线 / ...)+ 一个"待分配"区(release_train IS NULL)
2. **列 = lifecycle_state**:每个泳道内,story 按 lifecycle_state(开发/测试/上线/结项)分列。**只读,引擎推的,不拖**
3. **拖动归班车**:story 从"待分配"拖到某个班车 = 改 release_train。**只能横向拖(改班车归属),不能纵向拖(状态引擎管)**
4. **WIP 计数**:每个班车泳道显示"开发中+测试中"的 story 数,超限高亮(只提醒不阻断)

### P1(后续)

5. 班车实体化(升级成表,带计划日期/协同部门/快慢属性)
6. 班车日提醒(到点提示发车)
7. story 换班车(已在 v3.2,改拖到 v3.3)

### 非目标(明确不做,防无底洞)

- ❌ 拖动改 lifecycle_state(状态引擎控,人不能拖)
- ❌ 新 stage(不造 waiting_release,"等车"= lifecycle_state='上线' 自然语义)
- ❌ 新 profile(不造 fast/slow profile,profile 跟班车正交)
- ❌ 漂亮动画、复杂过滤、移动端适配
- ❌ WIP 硬阻断(只提醒)

---

## 3. 核心概念

### 3.1 两个正交维度(必须分清)

| 维度 | 字段 | 谁控 | 取值 | 看板角色 |
|---|---|---|---|---|
| **业务进度** | `lifecycle_state`(已有) | 引擎自动推 | 开发/测试/上线/结项 | **列**(只读展示) |
| **班车归属** | `release_train`(新增) | 人手动拖 | v3.2/v3.3/后台快线/催收线/NULL | **泳道**(可拖改) |

**铁律:lifecycle_state 不许人拖(引擎控);release_train 人随便拖(归班车)。**

### 3.2 "等车"的语义(不造新词)

`lifecycle_state='上线'` 且 `release_train` 指向某班车 = **等车**。例如:
```
lifecycle_state='上线', release_train='v3.2' → 等v3.2班车发车
lifecycle_state='上线', release_train='后台快线' → 等后台快线发车(可能很快)
```

**不需要 `waiting_release` stage。** "等车"是 `上线 + 有班车归属` 的自然组合,不需要建模成独立状态。

### 3.3 待分配区

`release_train IS NULL` 的 story 进待分配区。**同步进来的 story 默认 release_train=NULL**,先进待分配,人拖到班车。

---

## 4. 数据模型

### 4.1 新增(唯一改动)

story 表加一列:

```python
# models.py init_db() 里加幂等迁移(跟现有 driver_claim/lifecycle_state 同款写法):
try:
    conn.execute("ALTER TABLE story ADD COLUMN release_train TEXT")
except sqlite3.OperationalError:
    pass  # column already exists
```

加进 `VALID_COLUMNS`:
```python
VALID_COLUMNS = frozenset({
    ...,
    "release_train",   # 新增:班车归属(v3.2/v3.3/后台快线/NULL)
})
```

**取值约定**(字符串,不建表):
- `"v3.2"` / `"v3.3"` / ... —— App 包版本班车
- `"后台快线"` / `"催收线"` / ... —— 非版本班车
- `NULL` —— 待分配

**为什么用字符串不用 FK 表?**(P1 再升级)
- 当前班车数量少(<10),字符串够用
- 班车还没有需要管理的属性(日期/部门)——以后有需求再升级成 `release_train` 表 + FK

### 4.2 复用(零改动)

- `lifecycle_state` —— 已有,DEFAULT '开发',引擎推
- `story_states`(profile 配置)—— 已有,定义流转
- `advance_lifecycle_state` API —— 已有
- `lifecycleState` API 字段 —— 已暴露

### 4.3 不改的(明确)

- ❌ profile 不改(不造 fast/slow)
- ❌ story_states 不改(不造 waiting_release)
- ❌ stage 流水不改

---

## 5. 状态机(复用现有,不造新)

### 5.1 lifecycle_state 流转(完全复用代码)

```
开发(design,build) ──stages全done──► [confirm gate] ──advance API──► 测试
测试(verify)       ──stages全done──► [confirm gate] ──advance API──► 上线
上线(stages=[])    ──终态,等班车发车──► 结项(发车后人工/自动标)
```

这是 `planner.py:1028-1112` + `api.py:827` 已有的逻辑,**本设计一行代码不动它**。

### 5.2 release_train 流转(新增,简单)

```
[同步进来] → release_train=NULL(待分配)
                │
                │ 人拖到泳道
                ▼
        release_train='v3.2'(归班车)
                │
                │ (P1)拖到别的班车
                ▼
        release_train='v3.3'(换班车)
```

**没有状态机**,就是改字符串。拖到哪改哪。

### 5.3 两维度的组合(看板看到的)

| | lifecycle_state=开发 | lifecycle_state=测试 | lifecycle_state=上线 | lifecycle_state=结项 |
|---|---|---|---|---|
| release_train=v3.2 | 正在为v3.2开发 | v3.2要测的 | 等 v3.2 班车 | v3.2 已发 |
| release_train=后台快线 | 后台正在做 | 后台要测 | 等后台发 | 已发 |
| release_train=NULL | 待分配(在开发) | 待分配(在测) | 待分配(等车) | — |

**看板就是这张表的二维可视化**:泳道=行(release_train),列=列(lifecycle_state)。

---

## 6. UI 设计

### 6.1 二维看板布局

```
┌─────────────────────────────────────────────────────────────────────┐
│  班车看板                                                  [新建班车]  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  📦 v3.2 班车   WIP: 3   ▏计划: 7月底                                 │
│  ┌──────────┬──────────┬──────────┬──────────┐                      │
│  │ 开发     │ 测试     │ 上线 🚌  │ 结项 ✓   │                      │
│  │ [story D]│ [story E]│ [story F]│ [story G]│                      │
│  └──────────┴──────────┴──────────┴──────────┘                      │
│                                                                     │
│  ⚡ 后台快线    WIP: 2/3 ⚠️超限  ▏随时发                               │
│  ┌──────────┬──────────┬──────────┬──────────┐                      │
│  │ 开发     │ 测试     │ 上线     │ 结项     │                      │
│  │ [story]  │ [story]  │ [story]  │          │                      │
│  │ [story]  │          │          │          │   ← 超限:开发列 2 个  │
│  └──────────┴──────────┴──────────┴──────────┘                      │
│                                                                     │
│  📥 待分配      WIP: —                                              │
│  ┌────────────────────────────────────────────┐                      │
│  │ [story X(开发)] [story Y(测试)] [story Z]   │   ← 扁平,未归班车   │
│  └────────────────────────────────────────────┘                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**关键交互**:
1. **横向拖** = 改 release_train(从"待分配"拖到"v3.2",或从"v3.2"拖到"后台快线")
2. **纵向不能拖** = lifecycle_state 列是只读的,引擎推的
3. **story 卡片在哪个列** = 由 lifecycle_state 决定(只读),不由人摆

### 6.2 WIP 计数(只提醒)

```
WIP = 该班车泳道里 lifecycle_state IN ('开发','测试') 的 story 数
WIP limit: 初版不设硬上限,看板上显示数字 + 超过阈值(可配,默认 3)变红 ⚠️
不阻断:人有权超限,只视觉提醒
```

### 6.3 待分配区(扁平,不分列)

待分配区的 story 不按 lifecycle_state 分列(它们还没归班车,分列意义不大),扁平展示 + 标注每个的当前 lifecycle_state 即可。

### 6.4 新建班车

初版:前端"新建班车"= 输入一个名字(如 `v3.4`),存到一个简单配置(localStorage 或一个 settings 表)。**P1 才升级成 release_train 表**。

### 6.5 与现有 Web Board 的关系

- **新增一个页面/视图** `ReleaseTrainBoard`(不替换 Dashboard),作为"班车视角"补充
- **StoryList / Dashboard** 不动(保留现状)
- story 卡片复用现有组件
- advance API 复用(点"进入下一状态"还是现有那个)

---

## 7. 与现有代码的关系

| 现有模块 | 变更 | 说明 |
|---|---|---|
| `db/models.py` `story` 表 | **加一列** `release_train TEXT` + 加进 VALID_COLUMNS | 唯一 schema 改动 |
| `db/models.py` `init_db()` | **加幂等迁移**(同 driver_claim 写法) | `ALTER TABLE ... ADD COLUMN` |
| `db/models.py` CRUD | **无改动**(update_story 已通用,VALID_COLUMNS 加了就行) | |
| `orchestrator/service/api.py` | **加字段暴露**:story 返回里加 `"releaseTrain": s.get("release_train")` | 跟 lifecycleState 同款 |
| `orchestrator/service/api.py` | **加端点**:`PUT /api/story/{key}/release-train` 改班车归属 | 新增,简单 |
| profile / story_states / advance | **完全不动** | 复用 |
| `frontend/src/api/client.ts` | **加类型** `releaseTrain?: string` + `setReleaseTrain(key, train)` | |
| `frontend/src/pages/` | **新增** `ReleaseTrainBoard.tsx` | 新视图 |
| `frontend/src/components/` | **新增** `Swimlane.tsx` / `WipBadge.tsx` | |
| Dashboard / StoryList | **不动** | 保留现状 |

---

## 8. Phase 划分

### Phase 1:数据层(后端)
**改动**:
- `models.py`:加 `release_train` 列 + 幂等迁移 + VALID_COLUMNS
- `api.py`:story 返回加 `releaseTrain` 字段
- `api.py`:新增 `PUT /api/story/{key}/release-train`(body: `{train: "v3.2"}`)

**完成标志**:
- [ ] DB migration 幂等(老库升级不出错)
- [ ] `GET /api/story` 返回带 `releaseTrain`
- [ ] `PUT /api/story/{key}/release-train` 能改 release_train
- [ ] 同步进来的新 story release_train=NULL(进待分配)

### Phase 2:看板 UI(前端)
**改动**:
- 新增 `ReleaseTrainBoard.tsx`:二维看板(泳道=release_train,列=lifecycle_state)
- 新增 `Swimlane.tsx` / `WipBadge.tsx`
- 横向拖动改 release_train(调 setReleaseTrain)
- 纵向 lifecycle_state 列只读展示(调现有 advance API 推进,不拖)

**完成标志**:
- [ ] 看板按 release_train 分泳道,待分配区在底部
- [ ] 每个 story 在正确的 lifecycle_state 列(只读,引擎推)
- [ ] 横向拖动改班车,持久化
- [ ] WIP 计数显示,超限变红 ⚠️

### Phase 3(非目标,不做):班车实体表 / 班车日 / 拖回状态 / 漂亮动画

**初版上线 = Phase 1 + Phase 2 完成。** 后续慢慢维护。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| release_train 用字符串,班车多了乱 | 初版够用(班车<10);P1 升级成表 + FK,演进路径清楚 |
| 班车名拼错(v3.2 vs v3.2.0) | 前端从已知列表选(不手输),或自动归一 |
| lifecycle_state 跟班车语义混淆 | 文档铁律:lifecycle_state 引擎控(不拖),release_train 人控(可拖) |
| 同步覆盖 release_train | upsert_story_from_source **不写 release_train**(跟 intake_state 同理,本地字段不被同步覆盖) |
| 又变无底洞(builder 陷阱) | 初版 done = Phase 1+2,Phase 3 列非目标。硬边界 |

---

## 10. 评审决策(v0.2 已定)

| 问题 | 决策 | 理由 |
|---|---|---|
| 看板的列用什么? | **复用 lifecycle_state**(开发/测试/上线/结项) | 已有第一公民字段,引擎自动推,不造新词 |
| "等车"怎么表达? | **lifecycle_state='上线' + release_train 有值** | 自然组合,不造 waiting_release stage |
| 班车怎么建模? | **一个 release_train 字符串字段**(v3.2/后台快线/NULL) | 班车少,字符串够;P1 升级成表 |
| 拖动改什么? | **只改 release_train(横向拖)** | lifecycle_state 引擎控,不能拖 |
| 待分配区怎么实现? | **release_train IS NULL** | 简单,同步默认 NULL |
| WIP 怎么处理? | **只提醒不阻断**(变红+⚠️) | 人有权超限 |
| 默认 profile 要改吗? | **不改** | profile 跟班车正交,无关 |
| 跨班车迁移(换班车)? | **支持**(拖即可,改 release_train) | 就改个字符串 |
| lifecycle_state 能拖吗? | **不能** | 引擎内部控制,看板只读展示 |

---

## 11. 一句话总结

> 看板的列 = 复用 `lifecycle_state`(已有,引擎控,只读);看板的泳道 = 新增 `release_train`(人控,可拖)。**唯一 schema 改动是加一列。不造新 stage、不造新 profile、不造新状态词。** 初版 = Phase 1(后端加字段+端点)+ Phase 2(前端二维看板)。
