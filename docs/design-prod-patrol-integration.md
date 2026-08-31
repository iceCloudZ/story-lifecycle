# 设计：生产巡检（prod-patrol）× story-lifecycle 集成

> 2026-08-31 · 提出方：hc-all 巡检实践（story 1068018 / 1068738 / 1066924 / 1066769 / 1066915 五需求 × APP 1.2.32 灰度首轮巡检后沉淀）
> 结论先行：**可行**。现有 API（documents / gate-results / release-train，kind 与 gate_name 均为自由字符串）已能支撑最小闭环，零服务端改动即可用；本文 Phase 2 是把巡检升为一等公民的体验增强。

## 1. 背景与问题

多需求上线后的生产巡检目前由 `hc-all/docs/prod-inspection.md` 手工台账承载（每需求一节：巡检项/判定标准/巡检记录），AI 或人工按节执行。实测痛点：

1. **巡检项与 story 脱钩**：台账按需求名组织，不在 story-lifecycle 里，story 结项后接力者不知道"这个需求上线后要查什么"。
2. **没有"包"的聚合视图**：一次 APP 发版（如 1.2.32）携带多个后端需求，巡检要按包聚合，台账只能靠人工扫全表。
3. **轮次结果无结构化落点**：每轮巡检的结论散落在 markdown 表格里，无法按 story/包查询历史趋势。

## 2. 概念模型（三层）

```
release train（包，如 app-1.2.32）
 └── story（需求）                    ← release_train 字段已有
      ├── PatrolItem[]（查什么）       ← 新增：每需求的生产巡检项清单
      └── PatrolRun[]（查成什么样）     ← 新增：每轮巡检逐项结果
```

- **包**：复用现有 `release_train` 字段（班车泳道），命名约定 `app-<版本>`（如 `app-1.2.32`）。一次 APP 灰度/全量 = 一个 train。
- **巡检项**：story 维度登记，随 story 走；story 结项但观察期未满时依然可查。
- **轮次**：按包或按 story 执行一轮，逐项 PASS/FAIL/WAIVED + 观测值 + 证据引用。

## 3. Phase 1：零改动闭环（已验证可用，无需发版）

利用现有 API 的自由字段约定，hc-all 侧 `prod-patrol` skill 即可跑通：

| 事项 | 现有 API | 约定 |
|---|---|---|
| 登记巡检项 | `POST /api/story/{key}/context/documents` | `kind="patrol"`，`ref` 指向 story 证据目录的 `patrol.md`（清单本体是 markdown，带统一 frontmatter：story_key / package / kind） |
| 包归属 | `PUT /api/story/{key}/release-train` | `{"train": "app-1.2.32"}` |
| 轮次结果 | `POST /api/story/{key}/gate-results` | `stage="release"`，`gate_name="prod_patrol"`，`result=PASS/FAIL/WAIVED`，`summary`=关键数字，`evidence_ref`=巡检记录文件 |
| 包视图 | `GET /api/story` 全量拉取 | 客户端按 `releaseTrain` 过滤（243 story 全量 <1MB，可接受） |

已完成的 Phase 1 数据（迁移源就绪）：
- 5 个 story 的 `patrol.md` 已按统一 schema 建好（`D:/hc-all/story/<story-id>-<摘要>/patrol.md`），来源=prod-inspection.md 各节。
- 本设计提交时同步执行：5 story 登记 documents(kind=patrol) + 挂 train app-1.2.32 + 回写首轮 gate-results。

## 4. Phase 2：服务端一等公民（建议在 story-lifecycle 实现）

> **已实现（2026-08-31）**：三表（`patrol_item` / `patrol_run` / `patrol_run_item`，`infra/db/schema.py`）+ 读写层 `infra/db/patrol.py` + 路由 `orchestrator/service/routers/patrol.py`（五端点，camelCase 序列化）+ `GET /api/story` 列表带 `patrolSummary`（徽标数据源）+ 前端：story 详情「生产巡检」tab（`PatrolTab`）与班车看板卡片巡检徽标。测试 `tests/test_api_patrol.py`（12 例）。§4.4 落地取「前者」：服务端**不**镜像 gate，skill 回写 PatrolRun 时继续自行写一条 `prod_patrol` gate。

### 4.1 数据模型

```python
# 巡检项（story 维度，随 story CRUD）
class PatrolItem:
    id: int
    story_key: str          # FK -> story
    seq: int                # 稳序号，轮次结果引用
    name: str               # "ES错误面-hc-order"
    type: str               # es_error_scan | es_behavior | sql_count | nacos_read | api_probe | manual
    params: JSON            # 按 type 的查询参数（service/keyword/window / instance/db/sql / data_id/namespace/key / url/headers）
    baseline: str | None    # 噪音基线/阈值描述（如 "7d≈17条"）
    pass_criteria: str      # "错误≤基线、绕过=0"
    rollback_ref: str|None  # 回滚方式一句话
    enabled: bool

# 巡检轮次
class PatrolRun:
    id: int
    story_key: str
    run_scope: str          # train:app-1.2.32 | story 单跑
    started_at: datetime
    executor: str           # ai:prod-patrol | 人工:姓名
    summary: str            # 一行结论
    items: [PatrolRunItem]  # 逐项

class PatrolRunItem:
    item_seq: int
    result: str             # PASS | FAIL | SKIP | WAIVED
    observed: str           # 观测值（错误数/开关值/漏斗数字）
    evidence_ref: str       # 指向 hc-all 证据文件（patrol.md#巡检记录 等）
```

### 4.2 API（FastAPI，风格对齐现有）

| Method | Path | 说明 |
|---|---|---|
| PUT | `/api/story/{story_key}/patrol/items` | 批量替换该 story 的巡检项（全量覆盖，幂等） |
| GET | `/api/story/{story_key}/patrol/items` | 列表 |
| POST | `/api/story/{story_key}/patrol/runs` | 回写一轮（items 结果内联） |
| GET | `/api/story/{story_key}/patrol/runs?limit=20` | 历史 |
| GET | `/api/trains/{train}/patrol/overview` | **包维度聚合**：train 下全部 story 的 items 数、最新一轮时间/结论、FAIL 明细、从未巡检的 story 列表 |

包视图聚合是 Phase 2 的核心价值：巡检 skill 每轮开始时 GET overview 一次即得本轮范围，结束后逐 story POST runs，看板直接可读。

### 4.3 UI

- **班车看板**：泳道卡片加巡检徽标（最新轮 PASS/FAIL/从未巡检 + 时间）；点开=该 story 巡检项与轮次历史。
- **story 详情**：新增「生产巡检」tab（items + runs 时间线），复用 gate-history 的展示模式。

### 4.4 与现有生命周期的关系

- 不新增 lifecycle stage：巡检发生在 story `status=completed`/`上线` 之后、观察期内。`lifecycle/pending` 不驱动巡检（由外部 skill/cron 触发）。
- gate-results（Phase 1 约定）与 PatrolRun 并存不冲突：Phase 2 落地后 skill 改写 PatrolRun，同时**继续**写一条 `prod_patrol` gate（保持看板/时间线兼容），或由服务端在 PatrolRun 落库时自动镜像一条 gate（二选一，实现时定，推荐前者简单）。

## 5. 执行侧（hc-all skill，已建）

`D:/hc-all/.agents/skills/prod-patrol/`：

- 触发：上线巡检 / 生产巡检 / 灰度检查 / 按包巡检。
- 流程：解析范围（story_key 或 train，默认活跃 `app-*` train）→ 拉巡检清单（Phase 2=GET patrol API；Phase 1=documents kind=patrol → 本地 patrol.md）→ 执行标准动作（errors-scan / switch-check / funnel 双口径 / user-block-scan / 包内 app_version 分布）→ 回写（gate-results + patrol.md 追加 + prod-inspection.md 镜像）。
- 服务端不可用 fallback：直接扫 `D:/hc-all/story/*/patrol.md`，结果只落本地不丢。

## 6. 迁移与验收

- 迁移源：5 个存量 patrol.md（见 §3）。Phase 2 落库时由 skill 一次性 `PUT items` 导入（markdown → 结构化 params 手工映射，5 个 story 一次性工作）。
- 验收场景：
  1. 给 `app-1.2.32` 包跑一轮巡检 → 返回逐 story 结论 + 包汇总（全 PASS）。
  2. 把某 story 一项改成 FAIL → overview 聚合可见 FAIL 徽标与明细。
  3. 新需求上线 → 在 story 登记 items → 下一轮包巡检自动带上。
  4. server 停机 → skill fallback 本地 patrol.md 跑通（结果本地留存）。

## 7. 约定与已知坑（实现时必读）

- STORY_KEY 必须带 `tapd-` 前缀（裸 id 报 story not found）。
- 中文 body 一律 UTF-8 文件 + `--data-binary @file.json`（PowerShell/Git Bash inline 会走 GBK）。
- prod 环境只读：巡检动作仅 GET/curl 只读 + DB 只读 SELECT；Nacos 直读地址、ES 字段（`@timestamp`/`content`）、DMS 行取值（rows 是 list[list]）等工具坑已沉淀在 prod-patrol SKILL.md，服务端不直接碰生产。
- 包命名 `app-<版本>`；后端单独发版也可建 train（如 `be-2026-08-21`），与 APP 包解耦。
