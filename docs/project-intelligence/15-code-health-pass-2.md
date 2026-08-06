# 代码健康度治理（第二部分：god file 拆分 + 工具链 + Design-13 自身收敛）

> **状态**：待实施
> **前置**：设计 14（第一部分）已完成——死代码已删、STORY_HOME 已统一、spawn/prompt/taskkill/JSON 已合并、init_db 已拆、测试已校准。全量测试绿。
> **本部分范围**：**高风险、高收益**的三件事：(1) 装 CI 工具链防长回来；(2) 拆两个 god file（`models.py` 3111 行、`api.py` 4832 行）；(3) Design 13 自己引入的新债（`scheduler.py` 733、`executors.py` 694）收敛。
> **执行者**：自包含，但**强烈建议拆成 3-4 个独立 story/PR**，不要一个 commit 干完。每章是一个独立可交付单元。
> **预估工作量**：models.py 拆分 ~1 天 / api.py 拆分 ~2-3 天 / 工具链 ~半天 / Design-13 收敛 ~1 天。

---

## 0. 为什么本部分风险高

| 项 | 风险源 | 影响 |
|----|--------|------|
| `models.py` 拆分 | 被 **123 个文件** import；`get_story` 被调 246 次、`update_story` 160 次、`create_story` 105 次 | 改错 import 路径 → 全包崩 |
| `api.py` 拆分 | `app` 是 uvicorn 入口；`lifespan` 管编排线程；125 个端点路由注册顺序敏感；测试直接 import 私有 helper | 改错 → serve 起不来 / SPA 吞 API / 测试全红 |
| Design-13 新代码 | `scheduler.py`/`executors.py` 是**刚写的**，没有历史测试沉淀 | 改错 → 编排线程行为变 |

**核心原则**：每一步都用**门面层（facade）兼容旧 import**，不强制 123 个文件同步改。旧路径继续可用，新路径逐步迁移。

---

## 1. 工具链安装（第 0 步，先做）

### 1.1 装三个工具

```bash
cd D:/github/story-lifecycle/packages/story-lifecycle
pip install radon vulture pylint
```

| 工具 | 作用 | 本部分用法 |
|------|------|-----------|
| `radon cc` | 圈复杂度 | 拆分前后对比，确认函数没变复杂 |
| `radon mi` | 可维护性指数 | 拆分后应提升 |
| `vulture` | 死代码检测 | 拆分后扫残留死代码 |
| `pylint` | `too-many-arguments`/`too-many-lines` 等坏味道 | 定基线阈值，防长回来 |

### 1.2 采集基线（拆分前）

```bash
# 圈复杂度 TOP 20（拆分后对比）
python -m radon cc src/story_lifecycle -s -nb 2>/dev/null | head -20 > /tmp/baseline-cc.txt

# 可维护性指数
python -m radon mi src/story_lifecycle -s 2>/dev/null | tail -1 > /tmp/baseline-mi.txt

# 死代码（拆分后应减少）
python -m vulture src/story_lifecycle 2>/dev/null | wc -l > /tmp/baseline-dead.txt

cat /tmp/baseline-*.txt
```

### 1.3 定 CI 阈值（防长回来）

在 `pyproject.toml` 加（执行时根据基线定具体值）：

```toml
[tool.pylint."MESSAGES CONTROL"]
disable = "all"
enable = "too-many-arguments,too-many-lines,too-many-locals,too-many-branches"

[tool.pylint.'FORMAT']
max-args = 8          # 现基线 top 是 16，分阶段降：16→12→8
max-line-length = 120

[tool.radon]
# 无官方配置段，用 CI 脚本卡：
# cc 阈值：不允许新增 C 级（复杂度>15）函数；现有 C 级只减不增
```

**验收**：`pyproject.toml` 有配置；基线数据存在 `/tmp/baseline-*.txt`。

---

## 2. `models.py` 拆分（第 1 步，先做这个 god file）

### 2.1 为什么先 models 不先 api

- models 是**纯函数 DAL**，无副作用、无路由注册顺序问题
- 依赖图是**扁平的**：119 函数分 17 family，**family 间零交叉调用**（所有跨 family 边都指向 `shared._db`），拆分顺序几乎任意
- models 被 api 依赖；models 先稳定，api 拆分才有底气

### 2.2 拆分方案（门面 + 子模块）

**目录结构**（新建 `infra/db/` 子包，`models.py` 保留为门面）：

```
infra/db/
├── __init__.py          # 导出 db = models（兼容旧 import）
├── models.py            # 门面：re-export 所有子模块符号（保持 db.<fn> 可用）
├── connection.py        # shared: get_db_path, get_conn, _db, _validate_columns, VALID_COLUMNS
├── schema.py            # shared: init_db 拆出的 _create_*_tables（设计14已拆）+ init_db 入口
├── stories.py           # 24 函数（create/get/list/update/delete + driver claim）
├── sessions.py          # 9 函数
├── findings.py          # 9 函数
├── deliveries.py        # 4 函数
├── documents.py         # 6 函数（story_documents 表）
├── story_docs.py        # 9 函数（doc 版本子系统）
├── change_items.py      # 4 函数
├── decisions.py         # 3 函数
├── events.py            # 7 函数
├── traces.py            # 6 函数（LLM trace + token 用量）
├── projects.py          # 6 函数
├── workspaces.py        # 10 函数
├── story_project.py     # 7 函数（绑定 + worktree 占用）
├── runtime_facts.py     # 2 函数
├── learned_patterns.py  # 6 函数
└── context_revision.py  # 2 函数
```

### 2.3 family 划分（已核实，执行时照此分）

| family | 函数数 | 行数 | 公开函数 |
|--------|-------:|-----:|---------|
| shared/connection | 5 | 583 | `get_db_path`,`get_conn`,`init_db`（+`_db`,`_validate_columns` 私） |
| stories | 24 | 519 | create/get/find/list_*/update/delete/claim_driver/upsert* |
| workspaces | 10 | 127 | create/get/list/update/delete + init_state + list_by_ws |
| sessions | 9 | 173 | compute_session_id/get/list/upsert/set/complete/delete/update_trace |
| story_docs | 9 | 163 | upsert/get/confirm/version/rollback/search |
| findings | 9 | 103 | create/get/update/list + evidence |
| events | 7 | 83 | log_event/record_gate/get_recent/parse/is_adversarial |
| traces | 6 | 210 | log_llm_trace/call + token_usage（+pricing 私） |
| learned_patterns | 6 | 80 | create/get/update/list/find_relevant |
| documents | 6 | 99 | create/get/list/update/delete（+normalize 私） |
| projects | 6 | 86 | create/get/list/update/delete |
| story_project | 7 | 140 | bind/get/update/unbind（+worktree 占用 私） |
| deliveries | 4 | 93 | create/get/list/update |
| change_items | 4 | 80 | create/get/list/update |
| decisions | 3 | 92 | log/get/count |
| runtime_facts | 2 | 65 | upsert/get |
| context_revision | 2 | 20 | get/bump |

**关键依赖约束（仅 1 处）**：`init_db`（shared）调 `_backfill_llm_trace_story_keys`（traces）。拆分时把 `_backfill_llm_trace_story_keys` 移进 `connection.py`/`schema.py`（它是 schema 回填，语义本属 schema 层），或 `init_db` 内 lazy import。

**模块级常量迁移**（6 个，全部不可变，零风险）：
- `VALID_COLUMNS`(frozenset) → connection
- `MODEL_PRICING_CNY`(dict) → traces
- `COMPLETED_STATES`(frozenset) → stories
- `SEVERITY_ORDER`(dict) → findings
- `WORKSPACE_INIT_STEPS`(tuple) → workspaces
- `_DISPLACEABLE_STATES`(set) → story_project

### 2.4 门面层（保 123 个调用方零改动）

`models.py` 改成纯 re-export：

```python
"""DB 数据访问层门面。

历史：本文件曾是 3111 行的 god module。设计 15 拆分为 infra/db/ 子包。
本文件保留为门面，re-export 所有子模块符号，保证 `from ..infra.db import models as db; db.<fn>()` 零改动可用。
新代码请直接 import 子模块（如 `from ..infra.db.stories import get_story`）。
"""
# shared
from .connection import get_db_path, get_conn, init_db, VALID_COLUMNS
# stories
from .stories import (create_story, get_story, list_active_stories, ...)
# ... 每个 family 一行
```

**关键**：`_db` 也要 re-export（`scripts/migrate_docs_to_db.py` 私 import 了它）。

### 2.5 执行顺序（叶子优先）

```
① connection.py + schema.py（shared 先就位，其余依赖它）
② 任选 family 迁移（顺序无关，因 family 间零依赖）—— 建议 5 个一批，每批跑全量测试
③ models.py 改成门面 re-export
④ 全量测试
⑤ vulture 扫死代码
```

### 2.6 验证

```bash
# 1. 门面可用（零调用方改动）
python -c "from story_lifecycle.infra.db import models as db; print(db.get_story, db.create_story)"
# 应打印函数对象，无 ImportError

# 2. 私 import 仍可用
python -c "from story_lifecycle.infra.db.models import _db, init_db; print(_db, init_db)"

# 3. 全量测试
python -m pytest tests/ -q

# 4. 死代码应减少
python -m vulture src/story_lifecycle/infra/db/ 2>/dev/null

# 5. 圈复杂度对比
python -m radon cc src/story_lifecycle/infra/db/ -s -nb 2>/dev/null | head -10
# 不应有新的 C 级函数

# 6. 单文件最大行数（拆分目标：每个子模块 < 300 行）
find src/story_lifecycle/infra/db/ -name "*.py" | xargs wc -l | sort -rn | head -5
```

**验收**：123 个 import 调用方零改动；`models.py` 门面 ≤ 80 行（纯 re-export）；每个 family 子模块 < 300 行；全量测试绿。

---

## 3. `api.py` 拆分（第 2 步，最高风险）

### 3.1 约束（不可违反）

| 约束 | 原因 |
|------|------|
| `app` 必须留在 `orchestrator/service/api.py` | uvicorn import 字符串 `story_lifecycle.orchestrator.service.api:app`（`entry/cli/main.py:502/540`） |
| `lifespan` 必须留在 `api.py` | 它管 Design 13 编排线程 + `cleanup_all`，是 app 级生命周期 |
| SPA catch-all `/{path:path}` 必须**最后注册** | 否则吞掉真 API 路由 |
| 4 个私有 helper 须从 `api.py` re-export | 测试直接 import：`_story_list_json`、`_build_interactive_stage_prompt`、`_build_stage_launch_prompt`(注:设计14-DD2 可能已删)、`_spawn_story_agent_pty` |

### 3.2 拆分方案（APIRouter + 门面）

**目录结构**：

```
orchestrator/service/
├── api.py              # 瘦壳：app 创建 + lifespan + include_router + SPA 路由 + re-export
├── _shared.py          # 跨 domain 共享 helper（见 3.4）
└── routers/
    ├── __init__.py
    ├── stories.py      # 8 端点（CRUD + meta）
    ├── sessions.py     # 11 端点（PTY + spawn + WS）—— 含 _pty_ws_handler
    ├── lifecycle.py    # 10 端点（状态机：advance/skip/fail/archive/abort）
    ├── context.py      # 11 端点（context get/put/refresh/pack/documents/branch）
    ├── plan.py         # 12 端点（start/plan/clarify/wait —— 最大 domain）
    ├── documents.py    # 10 端点（docs CRUD + version + rollback）
    ├── timeline.py     # 4 端点（timeline/gate/dependency-graph）
    ├── workspaces.py   # 12 端点（workspace-entities + profiles）
    ├── projects.py     # 3 端点
    ├── wiki.py         # 5 端点
    ├── bugs.py         # 8 端点
    ├── quality.py      # 6 端点（findings + approvals + review-feedback）
    ├── patterns.py     # 6 端点
    ├── deliveries.py   # 3 端点（delivery-artifacts）
    ├── deliverables.py # 3 端点
    ├── worktrees.py    # 3 端点
    ├── sync.py         # 2 端点（tapd sync）
    ├── intake.py       # 1 端点 + 3 helper
    ├── diagnostics.py  # 3 端点（health/loop-trace/debug）
    └── change_items.py # 1 端点
```

### 3.3 每个 router 的模板

```python
# routers/stories.py
from fastapi import APIRouter
from ...infra.db import models as db
from .._shared import _serialize_story_summary, _story_list_json  # 共享 helper

router = APIRouter(prefix="/api", tags=["stories"])

@router.get("/story")
def list_stories(...): ...

@router.get("/story/{story_key}")
def get_story(...): ...
```

瘦壳 `api.py`：

```python
# api.py
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    from ..infra.db.models import init_db
    from ..engine.graph import recover_orphan_stories
    from ..scheduler import get_orchestrator, stop_orchestrator
    from ...infra.terminal.pty import cleanup_all
    init_db()
    recover_orphan_stories()
    orch = get_orchestrator()
    try:
        yield
    finally:
        stop_orchestrator(orch)
        cleanup_all()

app = FastAPI(title="Story Lifecycle Manager", version="0.1.0", lifespan=lifespan)

# include routers
from .routers import (stories, sessions, lifecycle, context, plan, documents,
                      timeline, workspaces, projects, wiki, bugs, quality,
                      patterns, deliveries, deliverables, worktrees, sync,
                      intake, diagnostics, change_items)
for mod in (stories, sessions, lifecycle, context, plan, documents, timeline,
            workspaces, projects, wiki, bugs, quality, patterns, deliveries,
            deliverables, worktrees, sync, intake, diagnostics, change_items):
    app.include_router(mod.router)

# WS 路由（注册到 app，不进 router，因 WS 不支持 APIRouter prefix 兼容性好）
# ... /ws/stories, /ws/pty/*, /ws/story/*

# 静态前端 + SPA catch-all（必须最后）
if _WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(...))
    @app.get("/{path:path}")
    def spa_fallback(...): ...

# 门面 re-export（保测试零改动）
from ._shared import _story_list_json, _serialize_story_summary
from .routers.sessions import _build_interactive_stage_prompt, _spawn_story_agent_pty
# 注：_build_stage_launch_prompt 若设计14已删则不 re-export
```

### 3.4 共享 helper 抽到 `_shared.py`（关键 hazard）

跨 domain 使用的 helper，不能塞进任何单个 router，提到 `service/_shared.py`：

| helper | 行 | 用到它的 domain |
|--------|---:|----------------|
| `_serialize_story_summary` | 189 | stories, bugs, ws-broadcast |
| `_story_list_json` | 221 | stories, ws-broadcast + 测试 |
| `notify_story_update` / `_sync` | 227/238 | lifecycle, plan, sessions |
| `_load_tapd_config` | 2461 | sync, bugs, intake, plan + CLI（**4 domain + 1 CLI，最分散**） |
| `_resolve_workspace_or_404` | 3111 | workspaces, wiki |
| `_prepare_intake_prd_content` / `_load_story_source_snapshot` | 3970/4061 | intake, plan |
| `_get_story_documents` / `_get_story_change_items` | 4780/4790 | context, plan（注：`context/resolver.py` 已有同名副本，顺手去重） |

**PTY spawn cluster**（`_spawn_story_agent_pty` + 5 个兄弟，253-990 行，~740 行）是 sessions 和 plan/start 共用的核心——单独建 `service/_pty_session.py`，re-export 给 `api.py`。

### 3.5 执行顺序（domain 按风险升序）

```
① _shared.py + _pty_session.py 先抽（其余 router 依赖它们）
② 低风险 domain 先：diagnostics, change_items, deliverables, worktrees, deliveries（端点少、依赖浅）
③ 中风险：projects, wiki, patterns, sync, timeline, quality
④ 高风险：documents, bugs, workspaces（端点多/共享 helper 多）
⑤ 最高风险：sessions（PTY cluster）、plan（最大 domain，含 start）、lifecycle（状态机核心）
⑥ stories（与 lifecycle 物理交错在 1022-1650，最后拆，可能合并成一个 router）
⑦ api.py 改瘦壳 + re-export
⑧ SPA 路由确保最后注册
⑨ 全量测试 + 手测 serve 起得来
```

每拆一个 domain 一个 commit，每次全量测试绿才进下一个。

### 3.6 验证

```bash
# 1. app 仍可 import（uvicorn 入口）
python -c "from story_lifecycle.orchestrator.service.api import app; print(app)"

# 2. 测试 import 的私有 helper 仍在
python -c "from story_lifecycle.orchestrator.service.api import _story_list_json, _spawn_story_agent_pty"

# 3. 路由数不变（125 个）
python -c "from story_lifecycle.orchestrator.service.api import app; print(len(app.routes))"

# 4. serve 起得来
cd D:/github/story-lifecycle/packages/story-lifecycle
python -m pytest tests/ -q
# 然后手动起 serve，curl 几个端点确认 200

# 5. 单文件最大行数
find src/story_lifecycle/orchestrator/service/ -name "*.py" | xargs wc -l | sort -rn | head -5
# api.py 应 < 200 行，每个 router < 400 行
```

**验收**：`api.py` ≤ 200 行；每个 router ≤ 400 行；125 个路由不变；`app` 可 import；serve 起得来；全量测试绿；SPA fallback 仍在最后。

---

## 4. Design-13 新代码自身收敛（第 3 步）

Design 13 引入了新文件，自己也长了坏味道：

### 4.1 `scheduler.py`（733 行）

| 问题 | 处置 |
|------|------|
| `_tick_stuck_check` 9 参数 | 收成 `StuckCheckInput` dataclass |
| `_tick_story` 嵌套深 | Extract Function |
| `_judge_task` + `_tick` + `_tick_story` + `_tick_stuck_check` + `_tick_alive_pty` 全在一个类 | 考虑把 judge 提交逻辑拆到 `JudgeDispatcher` 类，tick 循环保持瘦 |

### 4.2 `executors.py`（694 行）

| 问题 | 处置 |
|------|------|
| `spawn` 函数 180 行（headless + PTY 两分支） | 拆成 `_spawn_headless_path` + `_spawn_pty_path`，`spawn` 只做分发 |
| `InteractiveStageExecutor` / `AutomaticStageExecutor` 各 ~150 行 | 抽 `BaseStageExecutor` 共用逻辑到设计13已有的 `abc.StageExecutor`（Template Method） |
| `_spawn_headless` 9 参数 | 收成 `SpawnRequest` dataclass |

### 4.3 `stage_completion.py`（649 行）

| 问题 | 处置 |
|------|------|
| `judge_stage_completion` 217 行 + 15 参数 | 设计14-F2 已做 dataclass；函数体 Extract Function（`_call_llm`/`_parse_decision`/`_apply_decision`） |
| `_build_prompt` 149 行 + 13 参数 | 同上，prompt 组装段抽成独立函数 |

### 4.4 收敛原则

- **不改行为**：只改结构（Extract Function / 参数对象 / Template Method）
- **测试兜底**：`test_scheduler.py`、`test_executors.py`、`test_handlers.py`、`test_judge_three_decisions.py`（设计14新增）全程绿
- **每文件拆完跑 radon cc**：确认圈复杂度下降

### 4.5 验证

```bash
# 1. 圈复杂度对比基线
python -m radon cc src/story_lifecycle/orchestrator/scheduler.py src/story_lifecycle/orchestrator/executors.py src/story_lifecycle/orchestrator/evaluation/stage_completion.py -s 2>/dev/null

# 2. 全量测试
python -m pytest tests/ -q

# 3. 单函数最大行数
python -c "
import ast
for f in ['src/story_lifecycle/orchestrator/scheduler.py','src/story_lifecycle/orchestrator/executors.py','src/story_lifecycle/orchestrator/evaluation/stage_completion.py']:
    tree=ast.parse(open(f).read())
    longest=max(((n.end_lineno-n.lineno) for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))), default=0)
    print(f, longest)
"
# 目标：每个文件最长函数 < 80 行
```

---

## 5. 其余坏味道（第 4 步，随拆分顺手做）

| 编号 | 坏味道 | 随哪步做 |
|------|--------|---------|
| D6 | LLM 调用脚手架重复 ~34 处 | models/api 拆完后，统一走 `call_llm`/`call_llm_json` |
| D7 | robust-JSON 解析器 3 个 | 随 D2（设计14）后，统一到 1 个 |
| M2 | magic sleep 散落 | 拆 router 时提常量 |
| M4 | VISION_MODELS 硬编码 2 处 | 提到 config |
| I1 | pathlib/os.path 混用 | 拆分时统一 pathlib |
| I3 | log f-string vs % | 拆分时统一 % |
| I2 | paths.py + story_paths.py 两模块 | 设计14-D1 已合并 |

---

## 6. 全程验收清单（第二部分）

执行完所有步骤后，逐项打勾：

### 工具链
- [ ] `pip show radon vulture pylint` 三个都装了
- [ ] `pyproject.toml` 有 pylint 阈值配置
- [ ] 基线数据存档（拆分前后对比）

### models.py
- [ ] `infra/db/` 子包存在，17 个 family 子模块
- [ ] `models.py` 是门面，≤ 80 行，纯 re-export
- [ ] 123 个调用方零改动（`from ..infra.db import models as db; db.<fn>()` 可用）
- [ ] `_db` 私 import 仍可用
- [ ] 每个 family 子模块 < 300 行

### api.py
- [ ] `orchestrator/service/routers/` 子包存在，~20 个 domain router
- [ ] `api.py` 是瘦壳，≤ 200 行
- [ ] `app` 仍可 import（`uvicorn story_lifecycle.orchestrator.service.api:app`）
- [ ] 125 个路由不变（`len(app.routes)` 对比）
- [ ] `lifespan` 仍在 `api.py`，编排线程正常起停
- [ ] SPA catch-all 最后注册
- [ ] 测试 import 的私有 helper（`_story_list_json` 等）仍可从 `api.py` import
- [ ] serve 起得来，curl 几个端点 200

### Design-13 收敛
- [ ] `scheduler.py`/`executors.py`/`stage_completion.py` 每个最长函数 < 80 行
- [ ] 9 参数以上的函数已收成 dataclass
- [ ] 圈复杂度对比基线下降

### 整体
- [ ] 全量测试绿
- [ ] vulture 死代码数 ≤ 基线
- [ ] `find src/ -name "*.py" | xargs wc -l | sort -rn | head -5` —— 没有文件 > 1000 行

---

## 7. 给执行者的提醒

1. **拆成多个 PR**。models 一个、api 一个（甚至 api 按 domain 风险分 2-3 个）、工具链一个、Design-13 收敛一个。不要一个大 PR。

2. **门面层是安全网**。models.py 和 api.py 都保留为门面/re-export，让 123+ 个调用方零改动。新代码逐步迁移到直接 import 子模块，旧代码不强制改。

3. **每拆一个 domain 跑全量测试 + 手测 serve**。api 拆分的最大风险是路由注册顺序和 SPA 吞噬。每步都要起 serve 确认端点还在。

4. **WS 路由特殊处理**。FastAPI 的 `@app.websocket` 在 APIRouter 里行为略不同（prefix 问题），建议 WS 路由留在 `api.py` 直接注册，或单独验证 APIRouter 的 WS 行为。

5. **`_load_tapd_config` 是最分散的共享 helper**（4 domain + CLI）。务必先抽到 `_shared.py`，否则拆 router 时会到处复制。

6. **遇到测试红，优先怀疑是 import 路径或 re-export 漏了**，不是逻辑错。本部分不改行为，逻辑层不应该红。

7. **设计14 的 regression 守卫（`test_no_legacy_scheduling_regression.py`）全程必须绿**——它是编排线程不被破坏的最后防线。

---

## 8. 附录：与设计 14 的关系

| 项 | 设计14（第一部分） | 设计15（本部分） |
|----|------------------|----------------|
| 死代码 DD1-3 | ✅ | |
| STORY_HOME D1 | ✅ | |
| 小合并 D2/D5 | ✅ | |
| spawn D3 | ✅ | |
| prompt D4 | ✅ | |
| init_db 拆 F1 | ✅（函数内拆） | ✅（schema.py 子模块） |
| 长参数 F2（top2） | ✅ | ✅（DAL 的随 models 拆） |
| 测试校准 | ✅ | |
| 工具链 | | ✅ |
| **models.py god file** | | ✅ |
| **api.py god file** | | ✅ |
| **Design-13 新代码收敛** | | ✅ |
| D6/D7/M2/M4/I1/I3 | | ✅（随拆分） |

两份文档合计覆盖全部坏味道。设计14 是低风险卫生整治（先做），设计15 是高风险结构重构（后做，且拆多 PR）。
