# Prompt 归一化收尾 — 自包含执行文档

> **给执行者**：读完这一份就能独立完成 prompt 归一化的收尾，不需要问任何人。
> 全部步骤带「做什么 / 怎么验证 / 预期结果 / 测试」。每一步都自包含。
>
> **生成时间**：2026-08-06。基线 HEAD = `9f9d77f9`（砍 is_artifacts_ready 文件兜底）。

---

## 0. 先读：这件事的来龙去脉

### 0.1 为什么做归一化（事故根因）
真实跑测 story `tapd-1144381896001068018` 暴露两个缺陷：

**缺陷 1 — 完成判定靠"文件存在"**：编排线程每 5s 轮询 `is_artifacts_ready` 检查文件是否存在。但 claude 边写 spec.md 边存盘（渐进覆写），文件写一半就已存在 → 编排线程在 claude 还没写完时判"完成" → submit judge → judge 读到半成品 → 误 reject「成果物不完整」。

**缺陷 2 — 收尾动作前置**：`_submit_judge` 在 judge 出结果前就杀 PTY + 记 completed。reject 时 claude 已死，没法 resume 救场。

### 0.2 归一化做了什么（已 commit，你接手前已落地）
| commit | 内容 |
|---|---|
| `9d5491a4` | 缺陷2 修复：`_finalize_stage_pass` 收尾移到 approve 后 + `_judge_task` 透传 artifacts/evidence_candidates + 4 回归测试 |
| `c368b188` | prompt 入口三合一：`_build_cli_prompt` 迁入 `prompts.py`；删 `api._build_interactive_stage_prompt`/`_build_stage_launch_prompt`；16 参数收成 `CliPromptRequest` |
| `d2bdab4f` | **核心归一化**：declare 信号为单一真相源；A 组矛盾统一成"必须 declare"；spawn 快照 base_version 防旧 event；declare event 三处消费 |
| `8131ec66` | 删无 PTY 分支的 orphan 成果物兜底（虚假 approve 根因） |
| `9f9d77f9` | **砍 is_artifacts_ready 文件兜底**——spawn retry 间隙边写边判根因（本次收尾的基线） |

**核心变化（最重要的一句）**：
`is_artifacts_ready` 从"看文件存在"改成 **只认 `artifact_declared` event**（agent 调 `story tool declare` 时写）。agent 必须显式声明完成，编排器才判完成。彻底消除"边写边判"。

### 0.3 还剩什么（你要做的）
1. **同步一处 docstring 漂移**（abc.py 还写旧的"PTY 死文件兜底"口径）
2. **清死代码**（归一化没清的：`_build_plan_executor_prompt` 等 + 死模板）
3. **跑测试确认归一化不回归**
4. **（可选）真实重跑 1068018 端到端验证**

---

## 1. 执行前的环境确认（必做，5 分钟）

### 1.1 进对目录、用对 Python
```bash
cd D:/github/story-lifecycle
SERVE_PYTHON="C:/Users/zzh58/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
# 这个 venv 是 editable 安装了 story-lifecycle 的，测试用它
```

### 1.2 确认基线
```bash
git log --oneline -3
# 预期：看到 9f9d77f9（砍 is_artifacts_ready 文件兜底）在 HEAD 或近 HEAD
```

### 1.3 ⚠️ 工作区可能有 design-15 重构改动（与归一化无关）
**执行前必须先看 `git status`**。当前可能有约 70 个未提交文件，集中在 `routers/` 和 `infra/db/`——那是 **design-15 api.py 拆分重构**，**和 prompt 归一化无关**。

**处理原则**：
- 你的归一化收尾改动（abc.py / prompt_renderer.py / nodes / infra/prompts）**不要和 design-15 改动混在同一个 commit**。
- 暂存时只 `git add` 你改的归一化文件，别 `git add -A`。
- 如果 design-15 改动碰巧改了你要碰的文件（比如 routers 里的 prompt 相关），先 stash design-15 改动或等它 commit。

### 1.4 拿到当前测试基线（绿/红）
```bash
# 全包测试（约 4 分钟，开了 xdist 并行）
python -m pytest packages/story-lifecycle/tests -q 2>&1 | tail -5
```
**记录此时的 passed/failed 数**——这是你的回归基线。收尾后这个数只能持平或变好（删除的 orphan 测试除外）。

---

## 2. 收尾任务

### 任务 A：同步 abc.py docstring 漂移【必做，2 分钟】

**问题**：`is_artifacts_ready` 实现已经 declare-only（PTY 活/死都只认 declare，不文件兜底），但抽象基类 `abc.py:51-59` 的 docstring 还写旧的"PTY 死 → OR 文件兜底"口径。

**做什么**：
```bash
# 确认漂移存在
sed -n '51,59p' packages/story-lifecycle/src/story_lifecycle/orchestrator/abc.py
```
当前内容（旧口径，要改）：
```python
        """stage 成果物是否完成（agent 已显式声明）。

        归一化判定（1068018 事故修复）：
        - PTY 活（agent 还在跑）→ 只认 ``artifact_declared`` event（version > base），
          不看文件（防 agent 边写边存被误判完成）。
        - PTY 死/无（agent 不会再改文件）→ 认 declare event，OR 文件兜底
          （状态冻结可信，容错 agent 崩溃/declare 失败）。
        - base_version：spawn 时快照的最新 declare version，过滤上轮残留 event。
        """
```

改成（与 executors.py:156-165 实现一致）：
```python
        """stage 成果物是否完成（agent 已显式声明）。

        归一化判定（1068018 事故修复）：
        **只认 ``artifact_declared`` event**（version > base_version）。不看文件 ——
        agent 边写边存，任何"文件存在"判定都会撞上半成品。pty_alive 参数保留但
        不再分支：PTY 活/死都只认 declare。不调 declare 的 agent 卡到
        STAGE_TIMEOUT 判 failed（能 /plan/regenerate 重跑，比误判半成品 reject 安全）。
        - base_version：spawn 时快照的最新 declare version，过滤上轮残留 event。
        """
```

**验证**：
```bash
grep -A8 "def is_artifacts_ready" packages/story-lifecycle/src/story_lifecycle/orchestrator/abc.py
# 不应再出现"文件兜底"字样
```

**测试**：docstring 改动不影响行为，无需专门测试。任务 D 全包跑即可。

---

### 任务 B：清死代码【必做，每步带零调用验证】

归一化没清的纯死代码。**每个删除前都必须验证零调用者**（grep 全仓 + tests）。

#### B1. `prompt_renderer._build_plan_executor_prompt`（零调用者）

```bash
# 验证：除了自身定义和 nodes re-export，应无调用者
grep -rn "_build_plan_executor_prompt" packages/story-lifecycle/src packages/story-lifecycle/tests
# 预期：只有定义行（prompt_renderer.py:101）和 nodes/__init__.py 的 re-export
```
如果 grep 只命中这两处 → **删除 `prompt_renderer.py:101` 起的整个 `_build_plan_executor_prompt` 函数**。

#### B2. `prompt_renderer._build_stage_contract`（只被 B1 的死函数调）

```bash
grep -rn "_build_stage_contract" packages/story-lifecycle/src packages/story-lifecycle/tests
# 预期：定义行（prompt_renderer.py:72）+ 被 _build_plan_executor_prompt 调（:111）+ nodes re-export
```
B1 删了之后，这个函数也没调用者了 → **删除 `_build_stage_contract` 函数**。

#### B3. `nodes/__init__.py` 的 prompt_renderer re-export（零外部引用）

```bash
# 看 nodes/__init__.py 当前的 re-export 块
sed -n '25,40p' packages/story-lifecycle/src/story_lifecycle/orchestrator/nodes/__init__.py
```
当前 re-export 6 个：`_strip_planner_contract_duplicates` / `_build_stage_contract` / `_build_plan_executor_prompt` / `_render_prompt` / `_derive_relevance_tags` / `_build_prd_task_section`。

```bash
# 逐个验证外部引用（排除 nodes/__init__.py 自身和 prompt_renderer.py 定义）
for name in _strip_planner_contract_duplicates _build_stage_contract _build_plan_executor_prompt _render_prompt _derive_relevance_tags _build_prd_task_section; do
  echo "=== $name ==="
  grep -rn "$name" packages/story-lifecycle/src packages/story-lifecycle/tests | grep -v "nodes/__init__.py" | grep -v "prompt_renderer.py"
done
```
**判断**：
- `_render_prompt` 应该有外部引用（`entry/cli/main.py` dry-run 用）→ **保留**
- B1/B2 已删的两个 → 从 re-export 列表移除
- 其余的（`_strip_planner_contract_duplicates` / `_derive_relevance_tags` / `_build_prd_task_section`）：如果 grep 无外部引用 → 移除

**重要**：`nodes/__init__.py` 本身还有别的活引用（`load_profile` / `get_stage_config` 被 main.py/demo.py/story_service.py 用），**不要整个删 nodes 模块**，只清理 prompt_renderer 那块 re-export。

#### B4. 死模板文件（对应 stage 不存在）

```bash
# 验证这三个 stage 在任何 profile 里都不存在
grep -rn "^\s*test:\|^\s*review:\|^\s*review_design:" packages/story-lifecycle/src/story_lifecycle/entry/profiles/
# 预期：无输出（这三个 stage 名不存在于任何 profile）
```
无输出 → 删除三个死文件：
```bash
rm packages/story-lifecycle/src/story_lifecycle/infra/prompts/test.md
rm packages/story-lifecycle/src/story_lifecycle/infra/prompts/review.md
rm packages/story-lifecycle/src/story_lifecycle/infra/prompts/review_design.md
```

**B 任务验证**：
```bash
python -m pytest packages/story-lifecycle/tests -q 2>&1 | tail -5
# passed 数应与任务基线持平（删的是死代码，不影响活测试）
```

---

### 任务 C：处理 infra/prompts 旧协议模板【需决策，记录决策即可】

**现状**：`infra/prompts/design.md` / `build.md` / `verify.md` 仍写旧的 `.story/done/<key>/<stage>.json` 协议（让 agent 手写 JSON）。但生产 prompt 现在是 declare 协议（`_render_artifacts_obligation` + `build_done_protocol`）。

**关键事实**：这三个模板**只被 dry-run 路径加载**（`entry/cli/main.py` 的 `_render_prompt`，仅 `story create --dry-run` 跑）。生产路径（spawn）走 `prompts.py`，**根本不读这些 .md**。所以已隔离、不污染 agent。

**两个选择**（在文档或 commit message 里记录你选了哪个）：

| 选项 | 做法 | 适用 |
|---|---|---|
| (a) 改写 | 把 `design.md`/`build.md`/`verify.md` 的"完成后"段从 done.json 改成 declare 协议 | 如果 dry-run 输出仍要给人看、且要和生产一致 |
| (b) 标注 | 文件头加注释 `> ⚠️ dry-run 专用模板，完成协议与生产（declare）不一致，仅用于预览` | 如果 dry-run 只是开发调试用、不对外 |

**建议**：选 (b)，最低风险。在每个文件第一行加：
```markdown
<!-- dry-run 预览模板：完成协议与生产路径（declare）不一致，仅用于 story create --dry-run 调试。生产 prompt 见 prompts.py:_render_cli_prompt_req -->
```

---

### 任务 D：跑测试确认不回归【必做】

```bash
# 1. 归一化相关单测（快，几秒）
python -m pytest packages/story-lifecycle/tests/test_scheduler.py packages/story-lifecycle/tests/test_executors.py packages/story-lifecycle/tests/test_artifact_check.py -q

# 2. 全包（约 4 分钟）
python -m pytest packages/story-lifecycle/tests -q 2>&1 | tail -5
```

**通过判据**：
- `9d5491a4` 的 4 个回归测试全过：
  - `test_judge_receives_evidence_candidates`
  - `test_no_finalize_before_judge_result`
  - `test_finalize_on_approve_releases_pty`
  - `test_build_commit_only_on_approve`
- 全包 passed 数 ≥ 任务 1.4 记录的基线（删孤儿测试造成的减少除外）。
- `test_tick_judges_when_artifacts_ready` 应过（归一化后它用 `log_event("artifact_declared")` 替代写 spec.md）。

**如果红了**：看是不是你删死代码误伤了活引用 → grep 验证没做干净 → 恢复。

---

### 任务 E（可选）：真实重跑 1068018 端到端验证

这是归一化的终极验证。**只在任务 A-D 全绿后做**。

#### E1. 清数据 + 重启 serve
```bash
# 杀旧 serve
PID=$(netstat -ano | grep ":8180" | grep LISTENING | awk '{print $5}')
[ -n "$PID" ] && taskkill //PID $PID //F
sleep 2

# 清 1068018 所有 DB 数据
SERVE_PYTHON="C:/Users/zzh58/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
"$SERVE_PYTHON" -c "
import sqlite3
db = sqlite3.connect('C:/Users/zzh58/.story-lifecycle/story.db')
key = 'tapd-1144381896001068018'
for t in [r[0] for r in db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]:
    try:
        cols = [r[1] for r in db.execute(f'PRAGMA table_info({t})').fetchall()]
        if 'story_key' in cols:
            db.execute(f'DELETE FROM {t} WHERE story_key=?', (key,))
    except: pass
db.commit()
print('DB cleaned')
"

# 清文件
rm -rf D:/hc-all/.story/done/tapd-1144381896001068018
rm -rf "D:/hc-all/story/1144381896001068018-HC提现门槛判断规则更新"
rm -rf D:/hc-all/.story/context/tapd-1144381896001068018
rm -rf D:/worktrees/withdrawal-threshold-update

# 重启 serve（⚠️ 必须带 STORY_LLM_API_KEY，否则 judge 走 fallback）
cmd //c "set \"STORY_LLM_API_KEY=sk-67214fec289f450abeaf11329eebf3bc\" && cd /d C:\\Users\\zzh58\\AppData\\Local\\hermes\\hermes-agent && venv\\Scripts\\story.exe serve" > /tmp/serve_verify.log 2>&1 &
sleep 12
netstat -ano | grep ":8180" | grep LISTENING && echo "serve up" || echo "serve NOT up"
grep "orchestrator thread started" /tmp/serve_verify.log
```

#### E2. UI 重跑（intake → 规划 → 启动 CLI）
用浏览器开 `http://127.0.0.1:8180/`：
1. 点「新建并开始」→ 填 `1068018` → 点「读取需求」（等 30-60s，TAPD 拉取）
2. 勾选 hc-order / hc-limit / hc-coupon / hc-admin
3. 点「准备 PRD 并进入规划」（等规划完成，跳转详情页）
4. 详情页 design 阶段点「启动 CLI」

**IAB click 注意**：playwright `getByRole().click()` 在 IAB 长等待（>25s）后会 webview 掉线。用 `tab.dom_cua.click({node_id})` 点 label/button；用 bash 轮询 serve 日志判断后端进度，JS 调用秒级完成不持锁。

#### E3. 验证编排线程（核心判据）
claude spawn 后，用 bash 轮询：
```bash
KEY="tapd-1144381896001068018"
for i in $(seq 1 20); do
  sleep 25
  SUBMIT=$(grep -c "submit judge for stage=design" /tmp/serve_verify.log)
  DONE=$(grep -E "judge done stage=design" /tmp/serve_verify.log | tail -1)
  DECISION=$(grep -E "interactive (reject|approve) design|decision handled" /tmp/serve_verify.log | tail -1)
  echo "[$i] $(date +%H:%M:%S) submit=$SUBMIT done=$DONE decision=$DECISION"
  if [ -n "$DECISION" ]; then echo ">>> DONE"; break; fi
done
```

**通过判据**（对比事故时）：
| 验证点 | 事故时（修复前） | 应该是（修复后） |
|---|---|---|
| judge 触发时机 | claude 边写时（文件存在）→ 跑早了 | claude 调 `story tool declare` 后 → 时机正确 |
| judge 结果 | reject「成果物不完整」 | **approve**（读到完整 spec） |
| reject 后 claude | 被 `_release_stage` 杀死 | 不杀（reject 才不收尾；approve 才杀） |
| lifecycle | 卡 paused | 推进到 build |

**关键**：这次 judge 应该 **approve**，因为 claude 写完 spec 后调 declare，编排线程收到 declare event（version > base）才判完成，judge 此时读到的 spec 是完整的。

#### E4. 如果 claude 不调 declare（卡 STAGE_TIMEOUT）
这是**归一化后的有意行为**：不调 declare 的 agent 卡 timeout 判 failed，能 `/plan/regenerate` 重跑，比误判半成品 reject 安全。但正常情况 prompt 明确告诉 agent "必须 declare"，claude 会调。

如果确实卡了，看 claude transcript（`~/.claude/projects/D--worktrees-.../<sid>.jsonl`）确认它有没有调 declare、调失败了还是没调。

---

## 3. 提交（任务 A-D 完成后）

```bash
# ⚠️ 只 add 归一化收尾的文件，别卷入 design-15 重构改动
git add packages/story-lifecycle/src/story_lifecycle/orchestrator/abc.py
git add packages/story-lifecycle/src/story_lifecycle/orchestrator/engine/prompt_renderer.py
git add packages/story-lifecycle/src/story_lifecycle/orchestrator/nodes/__init__.py
git add packages/story-lifecycle/src/story_lifecycle/infra/prompts/  # 如果删了/改了模板

git commit -m "chore(prompt): 归一化收尾 — 同步 abc.py docstring + 清死代码

- abc.py is_artifacts_ready docstring 同步成 declare-only（砍文件兜底后口径）
- 删 prompt_renderer._build_plan_executor_prompt（零调用者）
- 删 prompt_renderer._build_stage_contract（只被上面的死函数调）
- 清 nodes/__init__.py 的 prompt_renderer 死 re-export（保留 _render_prompt）
- 删 infra/prompts/{test,review,review_design}.md（对应 stage 不存在）
- infra/prompts/{design,build,verify}.md 标注 dry-run 专用（与生产 declare 协议不一致）

归一化核心（is_artifacts_ready declare-only / declare event 消费 / prompt 协议统一）
已在 9f9d77f9 等 commit 落地，本次只清残留死代码 + 同步文档。"
```

---

## 4. 已知环境坑（重跑时参考）

| 坑 | 解法 |
|---|---|
| serve 没配 `STORY_LLM_API_KEY` → judge 走 fallback（不调真 LLM） | 重启时 `cmd //c "set \"STORY_LLM_API_KEY=sk-...\" && story.exe serve"`，引号包裹无尾随空格 |
| claude session "already in use" → 秒退 | spawn 自动 retry `--resume`（已实现）；或先杀残留 claude 进程 |
| claude 把 spec 写到 evidence 目录而非 worktree | `resolve_stage_artifacts` 的 evidence_candidates fallback 已处理 |
| IAB 浏览器长等待（>25s）webview 掉线 | 用 bash 轮询 serve 日志判断后端进度，JS 调用秒级完成不持锁 |
| 工作区有 design-15 重构改动（~70 文件） | 与归一化无关，提交时只 add 归一化文件，别 `git add -A` |
| pytest 残留 claude 进程（测试起真实 claude 没清理） | `tasklist \| grep claude` 看到无关 session（含 SYNC-1/pytest 字样）就 `taskkill //PID <pid> //F` |

---

## 5. 关键文件索引

| 文件 | 关键位置 | 说明 |
|---|---|---|
| `orchestrator/executors.py` | `is_artifacts_ready`:148-169（declare-only）；`resolve_stage_artifacts`:46-87 | 完成判定 + artifact 发现 |
| `orchestrator/scheduler.py` | `_tick_story` PTY 死分支:247-267；`_judge_task`:726；`_finalize_stage_pass`:667；base_version 快照:280-311 | 编排线程 |
| `orchestrator/prompts.py` | `_render_cli_prompt_req`:271；`_render_artifacts_obligation`:485-558 | prompt 单一入口（合并后） |
| `orchestrator/engine/task_actions.py` | `build_done_protocol`:192-243 | declare 协议措辞（与 obligation 段一致） |
| `orchestrator/engine/artifact_declare.py` | `declare_artifact`:72-212（写 event 在:189） | declare 实现 |
| `orchestrator/evaluation/stage_completion.py` | `collect_cumulative_outputs`:418-452 | judge 消费 declare |
| `infra/db/events.py` | `get_latest_declare`:49-74 | declare event 读接口 |
| `orchestrator/abc.py` | `is_artifacts_ready`:43-59 | **docstring 待同步（任务 A）** |
| `orchestrator/engine/prompt_renderer.py` | `_build_stage_contract`:72 / `_build_plan_executor_prompt`:101 | **死代码待删（任务 B1/B2）** |
| `orchestrator/nodes/__init__.py` | re-export:28-35 | **死 re-export 待清（任务 B3）** |
| `infra/prompts/{design,build,verify}.md` | 旧 done.json 协议 | **dry-run 隔离，待标注（任务 C）** |
| `infra/prompts/{test,review,review_design}.md` | 死文件 | **待删（任务 B4）** |

---

## 6. 历史背景（为什么会有这些死代码）

prompt 体系是三个时代的地层，归一化把前两代清成死代码/隔离：
- **前世代**：`prompt_renderer._render_prompt` 读 `infra/prompts/*.md`（done.json 协议）→ 现只剩 dry-run。
- **上世代**：`_build_plan_executor_prompt` + planner-packet 执行模型 + `_build_stage_contract` → 已被现世代取代，函数留壳。
- **现世代**：`prompts._render_cli_prompt` + prompt_sections + task_actions（declare 协议）→ 生产路径。

任务 B 是把地层最后铲平。

**两套工作流**（手动复制 prompt / PTY 自动驱动）**共用同一份 prompt 文件**，差别在 spawn 和完成通道，不在 prompt 本身——这是设计意图，不是 bug。手动工作流的 agent 在外部、没有 `STORY_*` 环境变量，prompt 里的绝对路径让它能直接 Write 文件；但**归一化后完成判定只认 declare**，所以手动工作流的 agent 也需要能调 `story tool declare`（在编排器环境的 agent 才有 declare CLI；纯外部 agent 若调不了 declare，会走 STAGE_TIMEOUT → failed → `/plan/regenerate`，这是有意取舍）。
