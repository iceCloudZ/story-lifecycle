# 代码健康度治理（第一部分：风险小重构 + 测试校准）

> **状态**：待实施
> **前置**：设计 13（全局编排线程，commit `4f8be4ef`）已落地，全量测试 1359 passed / 4 skipped
> **本部分范围**：**低风险卫生整治 + 测试对齐**。不动 `models.py`（3111 行）和 `api.py`（4832 行）这两个 god file——那是第二部分（设计 15）的事。
> **执行者**：自包含，任何 AI 拿着本文档可执行。每一步都有验证命令。
> **预估工作量**：代码改动 ~600-900 行 / 测试改动 ~1000 行 / 全程全量测试保持绿。

---

## 0. 背景与坏味道清单

设计 13 落地后做了一次全包 AST 审计（151 文件 / ~45.8K 行）。本部分处理其中**风险低、收益明确**的项；风险高的（god file 拆分、新代码自身收敛）留给设计 15。

完整坏味道清单见文末「附录 A」。本部分触及的子集：

| 编号 | 坏味道 | 本部分处置 |
|------|--------|-----------|
| DD1 | `orchestrator/nodes/__init__.py` 残骸常量（LangGraph 删除后的垃圾） | 删 |
| DD2 | `api.py:657 _build_stage_launch_cmd` 零生产调用，只 4 个测试用 | 删函数 + 删/改测试 |
| DD3 | `prd_generator.py:127 _parse_json_or_none` 纯转发 | 删，调用方直接用 `LLMClient._parse_json` |
| D1 | `STORY_HOME` 路径硬编码 20+ 处，仅 3 处认环境变量 | 抽 `story_home()` 助手，替换全部调用点 |
| D2 | `_extract_json_object` 括号计数 JSON 抽取器写两遍 | 合并到字符串感知版 |
| D3 | 两条 spawn 路径 resume 逻辑分叉 | 收敛到单一 spawn 函数 |
| D4 | 三个 prompt builder 还活着，`prompts.py` 迁移只做一半 | 完成 `prompts.py` 迁移，删旧 builder |
| D5 | `taskkill /T /F` 进程树杀 4 处 | 合并到 `platform_ops.kill_tree` |
| F1 | `init_db` 554 行单函数 | 按表族 Extract Function |
| F2 | 长参数列表（`judge_stage_completion` 15 参数等） | 收成参数对象 dataclass |
| T1 | 测试三分类（删 2 文件 / 改 4 文件 / 补新契约） | 见第 2 章 |

---

## 1. 执行顺序（重要：测试先行）

```
第 1 步：测试校准（第 2 章）          ← 先建安全网
第 2 步：死代码删除（第 3 章）        ← DD1/DD2/DD3，删完测试必须仍绿
第 3 步：STORY_HOME 统一（第 4 章）   ← D1，最高收益
第 4 步：小合并（第 5 章）            ← D2/D5，低风险
第 5 步：spawn 双路径合一（第 6 章）  ← D3，中风险，有回归测试兜底
第 6 步：prompt builder 三合一（第 7 章）← D4，中风险
第 7 步：init_db 拆分（第 8 章）      ← F1，纯机械
第 8 步：长参数 dataclass（第 9 章）  ← F2，纯机械
```

**每一步做完立即跑全量测试。任一步红，回退该步，不要带债推进。**

---

## 2. 测试校准（T1）—— 先做，建立安全网

### 2.1 删除：测已删除 poll loop 的文件（🔴）

Design 13 删掉了 `continue_orchestrator_agent` 内 1446 行的 poll loop。这两个文件测的就是那个 loop 的内部细节（mock `subprocess.Popen` / `_kill_headless` / `supervise_headless_stdout` / `_time.sleep`）：

| 文件 | 处置 | 行数 |
|------|------|------|
| `tests/test_story_state_machine.py` | **整文件删** | 596 |
| `tests/test_execution_mode.py` 里 2 个函数 | **删这 2 个函数，保留文件其余部分** | ~155 |

要删的两个函数（在 `test_execution_mode.py` 里精确定位）：
- `test_planner_interactive_spawn_passes_read_file_seed_not_full_prompt`
- `test_continue_verify_skips_gate_when_stuck_restart_no_artifacts`

> ⚠️ `test_execution_mode.py` 的**其余 6 个测试是 KEEP**（测 execution_mode 配置契约，与 poll loop 无关）。只删这两个函数，不要删整个文件。

**验证**：删完后 `python -m pytest tests/test_execution_mode.py -q` 应仍 pass（剩 6 个测试）。

### 2.2 改断言：4 个文件引用了语义变了的符号（🟡）

| 文件 | 改什么 | 估改比例 |
|------|--------|---------|
| `test_intake_boundary.py` | `start_story_async` 不再 spawn（只标 active）；`recover_orphan_stories` 语义转到编排线程。断言从「drove」改到「marked active + claim acquired」 | ~35% |
| `test_agent_api.py` | `/plan/confirm` 和 `/advance` 两个测试隐含「confirm→立即 drive」，改成断言「marked active」。`/answer`/`/wait`/`/plan`/`/archive` 不动 | ~20% |
| `test_driver_claim_cas.py` | 仅 docstring + 1-2 处注释框架（「start_story_async 的进程内字典」→ 现在是 drive_story_sync）。断言不变 | ~10% |
| `test_handoff_seed_context.py` | 删掉过时的「阻止 start_story_async 真起线程」注释框架。断言不变 | ~5% |

**改法**：逐个跑 `pytest tests/<file> -v`，看哪个失败，按失败信息把断言指向新语义。不要预先猜测。

### 2.3 补：Design 13 新契约测试（缺失覆盖）

现有测试对 Design 13 新代码覆盖很薄。补这 5 类（每类 5-10 个用例，新建文件，不塞进旧文件）：

**新建 `tests/test_orchestrator_thread_lifecycle.py`**：
- OrchestratorThread `start()` 后 `_thread.is_alive()` 为 True，`stop()` 后 join 成功
- `_tick` 遍历 active stories（mock `db.list_active_stories` 返回 2 个，断言都被 tick）
- `_tick_story` 检测 PTY 死活（mock `get_pty` 返回 alive/dead，断言分支）
- `_judge_task` 把结果写入 DB（mock `judge_stage_completion`，断言 `db.update_*` 被调）
- `stop()` 后线程池 `shutdown(wait=True)`，无残留任务
- 多次 `start()/stop()` 不泄漏线程

**新建 `tests/test_judge_three_decisions.py`**：
- mock LLM 返回 `{"quality":"approve","lifecycle_target":["开发"],"summary":"..."}`，断言 `StageCompletionDecision` 三个字段都对
- quality=reject → 不调 `advance_lifecycle_to_target`，插入 retry action
- quality=escalate → paused 等人
- lifecycle_target 跨多状态（`["开发","测试"]`）→ `advance_lifecycle_to_target` 迭代推进，遇 ui_button 暂停
- LLM 返回非法 JSON → 降级（默认 reject 或 escalate，不崩）

**新建 `tests/test_pty_resource_release.py`**：
- spawn 后 stage 正常退出 → PTY `alive=False`，`clean_exit_pty` 被调
- spawn 后 stage 异常 → try/finally 仍释放 PTY（用 `pytest.raises` 触发异常，断言 PTY 释放）
- stage 退出后 `db.delete_session` 被调（无孤儿 session 记录）
- marker 文件被 unlink

**新建 `tests/test_drive_story_sync_convergence.py`**：
- story 进入 paused → `drive_story_sync` 返回 "paused"，不再 tick
- story 进入 completed/failed → 同上
- 达到 max_rounds 仍 active → 返回当前 status（不卡死）
- force_auto=True vs False → executor 选 Interactive vs Automatic

**新建 `tests/test_no_legacy_scheduling_regression.py`**（regression 守卫）：
- `grep` 断言源码里**不存在** `consume_orphan_artifacts`/`consume_orphan_done`/`find_ready_interactive_stories`/`resume_ready_interactive_stories`/`_watch_interactive_done_files` 符号定义
- 用 ast 扫描 `planner.py`，断言 `continue_orchestrator_agent` 函数体 ≤ 30 行（防 poll loop 被偷偷加回）

> 最后这个文件是**最重要的防回归网**——防止未来有人把删掉的调度机制又加回来。用源码静态扫描实现，不依赖运行时。

### 2.4 测试校准的验收

```bash
cd D:/github/story-lifecycle/packages/story-lifecycle
python -m pytest tests/ -q --tb=short
```
**预期**：删除 2 文件 + 新增 5 文件后，总数 ≥ 1359 - 删除的用例数 + 新增用例数，全绿。

---

## 3. 死代码删除（DD1 / DD2 / DD3）

### 3.1 DD1：删 `orchestrator/nodes/__init__.py` 残骸

该模块是 LangGraph 删除后的"re-export 残骸"。三个常量无人引用：
- `STORY_HOME`（只被 `tests/conftest.py` monkeypatch，生产无人用——改 conftest 直接 patch 真路径）
- `TIMEOUT_SECONDS = 30 * 60`（grep 全包零引用）
- `MAX_REVIEW_RETRIES = 3`（grep 全包零引用）

**做法**：
1. 读 `orchestrator/nodes/__init__.py`，确认它只剩 re-export 和这三个常量
2. 删三个常量定义
3. 改 `tests/conftest.py:35` 的 `monkeypatch.setattr(nodes_mod, "STORY_HOME", story_home)` —— 改成 patch 真正使用 STORY_HOME 的模块（D1 统一后只有一个入口）
4. 如果 `nodes/__init__.py` 删完只剩空 re-export，整个文件清空但**保留**（很多 `from ... import nodes` 依赖它存在）

**验证**：`python -c "from story_lifecycle.orchestrator import nodes"` 不报错；全量测试绿。

### 3.2 DD2：删 `_build_stage_launch_cmd`

`api.py:657`，零生产调用，只 `tests/test_session_resume.py` 4 处引用。已被 `_spawn_story_agent_pty`（api.py:718）取代。

**做法**：
1. 读 `api.py:657` 的函数体，确认它和 `_spawn_story_agent_pty` 的 NEW/RESUME 逻辑重复
2. 删函数
3. 读 `tests/test_session_resume.py` 的 4 个引用（L44/L49/L61/L75），这些测试**测的就是这个死函数**——按 2.1 的逻辑，这些测试也是 🔴，要么删要么改去测 `_spawn_story_agent_pty`

> 决策：`test_session_resume.py` 整文件审视。如果它**全部**测 `_build_stage_launch_cmd`，整文件删（session resume 契约由 `test_interactive_session_resume.py` 覆盖）；如果部分测别的，删相关函数。

**验证**：`grep -rn "_build_stage_launch_cmd" packages/` 应只在文档里出现。

### 3.3 DD3：删 `_parse_json_or_none`

`prd_generator.py:127`，函数体就是 `return LLMClient._parse_json(content)`。纯转发。

**做法**：删函数，grep 调用方，全部改成 `LLMClient._parse_json(content)` 直接调。

**验证**：`grep -rn "_parse_json_or_none" packages/` 零命中。

---

## 4. STORY_HOME 统一（D1）—— 最高收益

### 4.1 问题

`Path.home() / ".story-lifecycle"` 在 20+ 文件重新算，**只有 3 处认 `STORY_HOME` 环境变量**。sandbox/测试隔离靠这个环境变量，17 处不认 = 隔离静默失效。

重复点（grep `Path.home()` / `".story-lifecycle"` 全包）：
- 认环境变量的（正确）：`infra/db/models.py:70`、`orchestrator/service/api.py:2466`、`orchestrator/engine/profile_loader.py:10`
- 不认的（错误）：`planner.py:26`、`graph.py:23`、`prompt_renderer.py:19`、`debug_packet.py:302`、`diagnostics.py:154/325`、`events.py:238`、`ttyd.py:18`、`source_loader.py:22`、`nodes/__init__.py:40`、`main.py:23/43/361`、`doctor.py:383`、`sync_cmd.py:103`、`config.py:17`、`shell.py:12`

> ⚠️ 本节列的是审计时的快照，**执行时必须重新 grep**（代码可能已变）。grep 命令：`grep -rn "\.story-lifecycle\|Path\.home()" packages/story-lifecycle/src/`

### 4.2 做法

**已有两个路径助手模块**（`infra/paths.py` + `infra/story_paths.py`），先合并再统一入口：

1. 读 `infra/paths.py` 和 `infra/story_paths.py`，确认职责
2. 在 `infra/paths.py` 定义唯一入口：
   ```python
   def story_home() -> Path:
       """story-lifecycle 数据根目录。认 STORY_HOME 环境变量（sandbox/测试隔离）。"""
       env = os.environ.get("STORY_HOME")
       return Path(env) if env else Path.home() / ".story-lifecycle"
   ```
3. 若 `story_paths.py` 有类似函数，删掉，全部转发到 `paths.story_home()`
4. 全包 grep 替换：所有 `Path.home() / ".story-lifecycle"` 和 `os.environ.get("STORY_HOME", ...)` 内联 → `from ..infra.paths import story_home` + `story_home()`
5. **特殊处理**：`profile_loader.py:258-259` 在函数内又算了一遍，删掉内联计算，用 `story_home()`

### 4.3 验证（关键）

```bash
# 1. 只有一个定义点
grep -rn "def story_home" packages/story-lifecycle/src/   # 应只有 1 行

# 2. 不再有内联计算
grep -rn "\.story-lifecycle" packages/story-lifecycle/src/ | grep -v "infra/paths.py"  # 应只剩 paths.py 里的字面量

# 3. STORY_HOME 环境变量全局生效（这是修复的核心）
STORY_HOME=/tmp/test-isolation python -m pytest tests/test_path_safety.py -q

# 4. 全量测试
python -m pytest tests/ -q
```

> 第 3 步是核心验证：设 STORY_HOME 指向临时目录，确认没有任何代码写到 `~/.story-lifecycle`。`test_path_safety.py` 已有路径安全测试可扩展加这条断言。

---

## 5. 小合并（D2 / D5）

### 5.1 D2：JSON 抽取器合一

两个 `_extract_json_object`：
- `infra/json_helpers.py:17`（朴素版，不处理字符串内转义）
- `infra/llm_client.py:164`（字符串感知版，处理 `\"`）

**做法**：删 `json_helpers._extract_json_object`，`robust_json_parse` 改调 `llm_client` 的版本；或把字符串感知版提到 `json_helpers`，两处都从那 import。后者更合理（json_helpers 是更底层）。

**验证**：`grep -rn "_extract_json_object" packages/story-lifecycle/src/` 只有一个定义点。

### 5.2 D5：taskkill 合并

`taskkill /T /F` 进程树杀，4 处：
- `orchestrator/engine/planner.py:598`（`_kill_headless`）
- `orchestrator/engine/consult_runner.py:242`（`from .planner import _kill_headless`，docstring 自承技术债）
- `infra/terminal/pty.py:412`（`ManagedPty.kill` fallback）
- `infra/terminal/platform_ops.py:24`（`_kill_by_port_windows`）

**做法**：在 `infra/terminal/platform_ops.py` 加：
```python
def kill_tree(pid: int) -> bool:
    """Windows: taskkill /T /F /PID。非 Windows: os.killpg。"""
    if sys.platform == "win32":
        return subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], ...).returncode == 0
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
```
四处的 taskkill 逻辑全部改成调 `kill_tree`。`consult_runner.py:242` 的跨模块 import 私有函数的技术债一并消除。

**验证**：`grep -rn "taskkill" packages/story-lifecycle/src/` 只在 `platform_ops.py` 出现。

---

## 6. spawn 双路径合一（D3）—— 中风险

### 6.1 问题

两条 spawn 路径，resume/NEW 检测逻辑已经分叉（注释引 bug `tapd-1144381896001066735`）：
- `orchestrator/service/api.py:718 _spawn_story_agent_pty`
- `orchestrator/executors.py:346 _spawn_session`（PTY 分支 L442-525）

`executors.py:6` 的 docstring 自承"从 api.py `_spawn_story_agent_pty` 拷贝"。

### 6.2 做法

**决策方向**：`_spawn_story_agent_pty` 是 serve 路径用的（手动 spawn），`executors._spawn_session` 是编排线程用的（自动 spawn）。两者都要：
- resolve resume/NEW → `compute_session_id` → `adapter.start_session` → 写 marker → `ensure_agent_pty` → arm sid capture → mkdir cwd

**抽公共函数**到 `infra/terminal/spawn_recipe.py`（新文件）：
```python
def spawn_agent_pty(adapter, model, *, story_key, stage, workspace, 
                    session_id=None, resume=False, preseed=None) -> SessionSpec:
    """统一的 agent PTY spawn 配方。api.py 和 executors.py 都走这里。"""
    # mkdir cwd
    # compute_session_id (NEW/RESUME 分支)
    # adapter.start_session
    # 写 marker
    # ensure_agent_pty
    # arm sid capture
    ...
```

`_spawn_story_agent_pty` 和 `_spawn_session` 都瘦成薄壳，调 `spawn_agent_pty` + 各自的特定后处理（api 版有 resume 重试；executors 版有 supervisor 接线）。

### 6.3 风险防控

这是本部分**风险最高**的一步。必须有回归测试兜底：
1. **先确保 `test_interactive_session_resume.py`（259 行）全绿**——这是 spawn 路径的主要回归测试
2. **先确保 `test_executors.py`（185 行）全绿**——这是 executors 的契约测试
3. 改完后这两个文件**必须仍全绿**，断言不变
4. 加一个 `test_spawn_recipe_consistency.py`：断言 api 路径和 executors 路径走同一个 `spawn_agent_pty`，marker 文件格式一致

**验证**：
```bash
python -m pytest tests/test_interactive_session_resume.py tests/test_executors.py tests/test_spawn_recipe_consistency.py -q
python -m pytest tests/ -q  # 全量
```

---

## 7. prompt builder 三合一（D4）—— 中风险

### 7.1 问题

`orchestrator/prompts.py` 头注释说"统一三个重复的 prompt builder"，但三个旧 builder 还在用：
- `api.py:559 _build_interactive_stage_prompt`
- `api.py:631 _build_stage_launch_prompt`（注：这个就是 DD2 要删的死函数，若 3.2 已删则此处只剩两个）
- `planner.py _build_cli_prompt`

新 `StagePromptBuilder`/`LaunchSeedBuilder`（prompts.py:24）只被 `executors.py` 用，迁移做了一半。

### 7.2 做法

1. 读 `prompts.py` 现有的 `StagePromptBuilder`/`LaunchSeedBuilder`，确认它们覆盖了旧 builder 的所有分支
2. 找 `_build_interactive_stage_prompt` 和 `_build_cli_prompt` 的所有调用点（grep）
3. 逐个调用点改成用 `prompts.py` 的类
4. **parity 测试**：改之前，对每个旧 builder 跑一批输入，存输出快照；改之后，新 builder 输出必须一致。用 `test_prompts.py` 现有的 parity 测试扩展
5. 删旧 builder

**验证**：
```bash
python -m pytest tests/test_prompts.py tests/test_build_cli_prompt.py -q  # parity 测试
grep -rn "_build_interactive_stage_prompt\|_build_cli_prompt" packages/story-lifecycle/src/  # 零命中
```

---

## 8. init_db 拆分（F1）—— 纯机械

### 8.1 问题

`infra/db/models.py:98 init_db`，554 行单函数，~20 个 CREATE TABLE 块。

### 8.2 做法

按表族 Extract Function（不拆文件，只拆函数）：

```python
def init_db():
    """初始化所有表。按表族分块，每族一个 _create_*_tables。"""
    with _get_conn() as conn:
        _create_story_tables(conn)
        _create_session_tables(conn)
        _create_finding_tables(conn)
        _create_delivery_tables(conn)
        _create_doc_tables(conn)
        _create_change_item_tables(conn)
        _create_runtime_fact_tables(conn)
        _create_decision_tables(conn)
        _create_trace_tables(conn)
        _create_index_seeds(conn)
```

每个 `_create_*_tables(conn)` 是 30-80 行的私有函数，包含该族的 CREATE TABLE + 索引 + seed。

### 8.3 验证

```bash
# 1. init_db 体长 ≤ 30 行
python -c "import ast; tree=ast.parse(open('src/story_lifecycle/infra/db/models.py').read()); print([len(range(n.lineno, n.end_lineno+1)) for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='init_db'])"

# 2. 全量测试（DB 初始化逻辑必须完全等价）
python -m pytest tests/ -q
```

> 纯机械重构，**不改任何 SQL**，只是把 CREATE TABLE 语句搬到私有函数里。如果测试红了，说明搬迁过程中改了 SQL，回退重做。

---

## 9. 长参数 dataclass（F2）—— 纯机械

### 9.1 目标函数（top 5）

| 函数 | 参数数 | 位置 | dataclass 名 |
|------|--------|------|-------------|
| `_build_cli_prompt` | 16 | `planner.py:1079` | `CliPromptRequest` |
| `judge_stage_completion` | 15 | `stage_completion.py:71` | `JudgeRequest`（或复用 Design 12 的 `StageCompletionDecision` 的入参侧） |
| `upsert_story_from_source` | 15 | `models.py:1443` | `StoryUpsertInput` |
| `create_delivery_artifact` | 14 | `models.py:2522` | `DeliveryArtifactInput` |
| `_build_prompt`（judge 用） | 13 | `stage_completion.py:375` | 同 JudgeRequest |

### 9.2 做法

每个长参数函数：
1. 定义 `@dataclass` 参数对象（字段 = 原参数，类型注解照搬）
2. 函数签名改成 `(req: XxxRequest)` 单参数（或保留位置参数兼容 + 加 `req` keyword 路径，逐步迁移）
3. 所有调用点构造 dataclass 传入

**优先级**：先做 `judge_stage_completion` 和 `_build_cli_prompt`（这俩参数最多、最难读）。DAL 的 `upsert_*`/`create_*` 可放到第二部分跟 models.py 拆分一起做。

### 9.3 验证

```bash
python -m pytest tests/test_build_cli_prompt.py tests/ -q  # 改了哪个跑哪个 + 全量
```

> dataclass 化是纯参数打包，不改逻辑。测试断言不变。

---

## 10. 全程验收清单

执行完所有步骤后，逐项打勾：

- [ ] `python -m pytest tests/ -q` 全绿，用例数 ≥ 1359 - 删除 + 新增
- [ ] `grep -rn "_build_stage_launch_cmd\|_parse_json_or_none" packages/` 零命中（DD2/DD3）
- [ ] `grep -rn "def story_home" packages/` 只 1 行（D1）
- [ ] `STORY_HOME=/tmp/x python -m pytest tests/test_path_safety.py -q` 绿（D1 核心验证）
- [ ] `grep -rn "taskkill" packages/story-lifecycle/src/` 只在 `platform_ops.py`（D5）
- [ ] `grep -rn "_extract_json_object" packages/` 只 1 个定义点（D2）
- [ ] `_spawn_story_agent_pty` 和 `_spawn_session` 都调 `spawn_agent_pty`（D3）
- [ ] `init_db` 函数体 ≤ 30 行（F1）
- [ ] `judge_stage_completion` 参数 ≤ 3（含 JudgeRequest）（F2）
- [ ] regression 守卫 `test_no_legacy_scheduling_regression.py` 绿（防 poll loop 加回）

---

## 11. 附录 A：完整坏味道清单（本部分 + 第二部分对照）

| 编号 | 坏味道 | 本部分 | 第二部分(设计15) |
|------|--------|--------|----------------|
| DD1-3 | 死代码 | ✅ | |
| D1 | STORY_HOME 20 处 | ✅ | |
| D2 | JSON 抽取器双份 | ✅ | |
| D3 | spawn 双路径 | ✅ | |
| D4 | prompt builder 三份 | ✅ | |
| D5 | taskkill 四份 | ✅ | |
| D6/D7 | LLM 调用脚手架重复 | | ✅（随 llm_client 拆分） |
| F1 | init_db 554 行 | ✅ | |
| F2 | 长参数列表 | ✅（top 2） | ✅（DAL 的随 models 拆） |
| M2/M4 | magic number/model 集合 | | ✅ |
| **G1** | **models.py 3111 行 god file** | | ✅ |
| **G2** | **api.py 4832 行 god file** | | ✅ |
| **G3** | **scheduler.py 733 / executors.py 694（Design 13 新债）** | | ✅ |
| I1/I3 | pathlib/os.path、log 风格不一致 | | ✅（随拆分） |
| T1 | 测试三分类 + 补新契约 | ✅ | |

---

## 12. 给执行者的提醒

1. **每步跑全量测试**。本部分有 9 步，每步一个 commit，每个 commit 测试必须绿。
2. **不要合并步骤**。D3（spawn 合一）和 D4（prompt 合一）是中风险，单独 commit，单独 review。
3. **遇到测试红，先想"是我改错了还是测试该校准"**。本部分重构不改行为，测试红大概率是改错了；唯一例外是 🟡 4 文件（第 2.2 节），那些是测试本身该改。
4. **D1 是重中之重**。STORY_HOME 不统一，sandbox/测试隔离就是假的，所有测试的"隔离"都不可信。优先做对。
5. **regression 守卫文件不能省**。`test_no_legacy_scheduling_regression.py` 是防止未来回归的唯一手段，必须有。
