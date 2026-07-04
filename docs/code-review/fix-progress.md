# 4-AI 代码审计 · 修复进度

> 起点：`docs/code-review/audit-report.json`（41 findings = 19 blocking + 19 warning + 3 nit）
> 目标：修完全部 19 个 blocking，warning 按主题顺手清，nit 按心情
> 状态：**P1/P2/P3 已合并 main，P4/P5 待启动**

---

## 总进度

| 阶段 | 内容 | blocking | 状态 | commit |
|---|---|---|---|---|
| **P1** | 安全加固（路径穿越/命令注入/上传/swebench） | 5 (AI-1) | ✅ 已合并 main | `a350c7a7` + `aeccf1ac` |
| **P2** | 异常治理（miner 数据丢失 + adapter 吞异常 + tapd/quality） | 1 (AI-1) + 3 交叉 | ✅ 已合并 main | `2558eebf` |
| **P3** | 回归测试补全 | 9 (AI-4) | 🟡 1/9 完成（ISS-004），8/9 待做 | `b290189a` |
| **P4** | 架构 · Decider 纯化 | 3 (AI-2) | ⬜ 未启动 | — |
| **P5** | 架构 · 状态机建模 | 2 (AI-2) + 2 w/n | ⬜ 未启动 | — |

**测试状态**：main 全量 691 passed，1 pre-existing 失败（`test_smoke::test_packaged_and_root_profiles_consistent`，环境问题，repo 根无 profile 文件，与改动无关）。

---

## P1 已完成（5 blocking）✅

### P1.1 公共 helper（`infra/story_paths.py`）
- 新增 `safe_segment()` / `safe_story_path()` / `assert_within_workspace()` / `UnsafePathError`
- `_safe_segment` 改为 `strip("-_") + rstrip(".")`（保留 `.story` 前导点）
- 入口 `create_and_start_story` 在 trust boundary sanitize story_key

### P1.2 全量替换 story_key 路径拼接（24+ 处）
ttyd / snapshot / planner / entry / evaluator_loop / gate / events / diagnostics / worktree handler / knowledge paths / testing asserters / harness。

### P1.3 rmtree blast-shield
`story_service.py:107` / `testing/workspace.py:33` / `doctor_paths.py:77,97` 全部在 rmtree 前加 `assert_within_workspace`。

### P1.4 命令注入修复
- `ttyd.py`：`shlex.quote` ws/pf；launch_cmd 显示行 quote、执行行不 quote（保留多 token 分词）
- `bootstrap.py`：新增 `_ps_quote()` 对 PowerShell 单引号转义（仿照 `_copy_to_clipboard` 正确做法）
- `shell.py`：`repr()` 替代 f-string 拼 binary；`shlex.quote(tmp)`
- `platform_ops.py`：`shlex.quote` 每个 bash_arg
- `claude.py`：`switch_provider` 的 `bash -c` 注入体替换为 `return None`（死代码，仅测试调用，保留方法签名维持抽象契约）

### P1.5 上传文件名 + swebench
- `api.py:2238`：basename + 拒 `..`/`.` + resolve 边界检查
- `swebench.py`：click callback `_validate_run_id` 拒非 `[A-Za-z0-9._-]`/前导点；instance_id 逐条校验；`_assert_within` 守每个拼接

### 测试
`test_path_safety.py`（20 passed, 2 skipped）：safe_segment / safe_story_path / assert_within_workspace / 入口 sanitize / upload basename / swebench id / rmtree blast-shield。

---

## P2 已完成（1 blocking + 3 交叉）✅

### P2.1 miner store.py parse-first 顺序（AI-1 #5）
旧：DELETE → parse → maybe continue（异常被吞返回空 meta 时，旧记录已删新记录未插，数据永久丢失）。
新：**parse 先行**，meta 为 None 时 continue 跳过 DELETE，旧记录保住。

### P2.2 三个 adapter 吞异常
`claude.py:100` / `codex.py:76` / `kimi.py:86`：`except Exception: pass` → `except Exception as e: log.warning(...); return None, [], []`（符合 base.py 契约）。

### P2.3 其他静默 except
- `tapd_source.py:91`：拉子任务失败 → `log.warning`（原来静默丢任务）
- `quality.py:198`：LLM rerank 失败 → `log.warning` 退化原因（原来静默降级）

### 测试
`test_store_parse_failure.py`（5 passed）：三个 adapter 异常返回 None + store parse-first 保留旧记录 + 成功 parse 仍替换。

---

## P3 进行中（9 blocking，已完成 1/9）🟡

### ✅ 已完成：ISS-004（commit `b290189a`）
- `test_verify_gate_plan_summary.py`（3 tests）
- 顺手修了 `gate.py:204` 的 latent import bug（`..infra` → `...infra`，从未触发因为 quality_cfg 默认 disabled）

### ⬜ 待做（8 个）

| # | 任务 | commit | 测试文件 |
|---|---|---|---|
| ISS-006 | 相对 workspace 拒绝 | c4f970dc | 扩展 `test_workspace_validation.py`（传 `.`/`rel/path` 断言 `WorkspaceError`） |
| c466dbbd | intake preview 预生成分支复用 | c466dbbd | 扩展 `test_api_integration.py`（preview 返回 branch + start 复用，mock generate_branch 计数=0） |
| dcd1b49d | create_project get-or-create | dcd1b49d | 新 `test_project_get_or_create.py`（同 repo_path 调两次不抛 IntegrityError） |
| 6e48d445 | SPA _WEB_DIR 路径 | 6e48d445 | 扩展 `test_api_integration.py`（GET `/` 返回 html + `_WEB_DIR` 含 `entry`） |
| f6ed0e29 | kb.py UTF-8 stdout | f6ed0e29 | 新 `test_kb_cli.py`（story-miner，subprocess + GBK locale 断言可解码中文） |
| 38258d6b | classify unknown+retry | 38258d6b | 新 `test_classify_failure.py`（story-miner，normalize_type 返回 unknown + batch retry） |
| missing-mod | sourcing.planner 整包测试 | — | 新 `test_planner.py`（parse_phases / state round-trip / decompose_phase fake LLM） |
| missing-mod | miner.store/story_ingest 测试 | — | 新 `test_store_ingest.py`（init_db / _extract_title / _scan_stages / _iso） |

**继续时的关键模式**（从 ISS-004 学到）：
- conftest.py 已 auto-isolate DB（每测试 tmp_path + init_db）
- 函数内 lazy import 的 patch 要打 canonical 模块（如 `db_mod.get_open_findings`），不是 `gate_mod.db`
- API 测试用 `starlette.testclient.TestClient`
- LLM 全 mock，不依赖网络

---

## P4 未启动（3 blocking · Decider 纯化）⬜

按 AGENTS.md 硬规则：Decider 必须纯函数，副作用只能在 Handler。

| AI-2 finding | 文件:行 | 任务 |
|---|---|---|
| #4 | `shadow_router.py:70 detect_triggers` | 抽 `resolve_router_facts() -> RouterFacts`（只读 DB），`detect_triggers(state, stage_config, facts)` 变纯；3 处 `except: pass` 改 log |
| #5 | `policy_engine.py:218/255` | `evaluate_policy(action, risk, rejection_count: int)` 入参化；`evaluate_guarded` 移除内部 `_write_autonomy_trace`（返回 decision 由调用方写）；`_count_rejections` 改名 `resolve_rejection_count` |
| #1 | `gate.py:190 run_verify_gate` | 拆 `resolve_verify_facts` + 纯 `decide_verify_gate(facts)` + Handler `apply_verify_gate_outcome`（拥有 build_repair_packet / write_gate_report / db.log_event / context 变更）；保留 `run_verify_gate` 作 thin wrapper |

---

## P5 未启动（2 blocking + 2 warning/nit · 状态机）⬜

按 AGENTS.md：状态机改动必须**先定义 state×action 映射再动 Handler 副作用**。

| AI-2 finding | 任务 |
|---|---|
| #2 (blocking) | 新建 `orchestrator/states.py` 定义 `StoryRunState` enum（6 态）；实现 `resolve_story_run_state()` 单一 Resolver；`graph.py` 三入口（is_story_running / find_ready_interactive_stories / recover_orphan_stories）对齐读同一值 |
| #3 (blocking) | `apply_verify_gate_outcome`（P4 产出）里，gate 非 advance 时把 `decision_id` 写进 `context_json["last_gate_decision_id"]`；让 `_is_in_gate_wait` 死分支复活 |
| #6 (warning) | `worktree/resolver.py:55` 返回 tagged (ok/empty/error) 区分 git 失败 vs 空仓 |
| #7 (nit) | `review_feedback.py:277 decide_approval` → `handle_approval` |

---

## ⚠️ 工作目录卫生

main 工作目录有**别的会话留下的未提交改动**（不属于本任务）：
- `packages/story-lifecycle/src/story_lifecycle/infra/terminal/pty.py`（modified，加 `_taps`/supervisor）
- `packages/story-lifecycle/src/story_lifecycle/orchestrator/engine/supervisor.py`（新文件）
- `packages/story-lifecycle/tests/test_pty_tap.py`、`tests/test_supervisor.py`（新文件）
- `docs/autonomous-pipeline-fix-roadmap.md`（新文件）

合并 P1/P2/P3 时已确认这些**没有被误带入 commit**。建议你确认来源后 `git stash` 或单独处理。

---

## 继续启动命令

### 一键继续 P3（回归测试补全）

```bash
cd D:/github/story-lifecycle
git checkout main
git pull
git checkout -b fix/p3-regression-tests-continue

# 激活 venv
source .venv-monorepo-test/Scripts/activate

# 然后对 AI 说：
```

**给 AI 的 prompt（粘贴下面这段）：**
```
继续 docs/code-review/fix-progress.md 里的 P3 回归测试补全（已完成 ISS-004，还剩 8 个）。

先读 docs/code-review/fix-progress.md 了解整体进度，再读 docs/code-review/ai-4-testing-performance-report.json
找具体 finding 和 AI-4 给的测试建议。按 fix-progress.md P3 待做表里的顺序逐个实现。

关键约束：
- 每写一个测试文件立即 `python -m pytest <file> -v` 必须全绿
- conftest.py 已 auto-isolate DB；lazy import 的 patch 打 canonical 模块
- LLM 全 mock，不依赖网络
- 发现 latent bug 顺手修并在 commit message 说明
- 8 个全做完跑 `python -m pytest packages/story-lifecycle/tests/ packages/story-miner/tests/ -q` 确认 0 回归
- 完成后 merge 到 main
```

### 继续的优先级建议

1. **P3 先做完**（纯加测试，零风险，且能兜底 P4/P5 的重构）—— 用上面的命令
2. **P4 Decider 纯化**（中风险，P3 测试齐了再动）—— prompt 类似，让 AI 读 fix-progress.md 的 P4 节
3. **P5 状态机建模**（高风险，最后做）—— 同样读 P5 节，**必须先评审 state×action 映射表再写代码**

### 一键继续 P4/P5（P3 完成后）

P3 完成后，把上面 prompt 里的 "P3" 换成 "P4"（或 "P5"），其余不变。AI 会读 fix-progress.md 自己找到任务。
