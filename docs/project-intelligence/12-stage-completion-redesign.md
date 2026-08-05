# Stage 完成裁判重构：一次 LLM 三个决定 + PTY 释放 + UI 摘要

> **状态**：已实施（2026-08-05，commit 见 git log）
> **背景**：真实跑需求（TAPD 1068018）暴露半自动模式每个 stage 推进都卡顿，根因是 lifecycle 推进判据硬编码（stage 名匹配 + 交付物清单）、PTY 不释放导致孤儿线程、用户无法看到每轮干了什么。
> **原则**：推进与 stage 状态解耦——LLM 做裁判，不硬编码；PTY 一定释放；用户看得见。

---

## 问题清单（真实跑出来的）

| # | 问题 | 根因 |
|---|------|------|
| 1 | single-pass（1 stage）跑完 lifecycle 卡死 | `_stages_done` 硬编码查 stage 名匹配，profile/source 错配时永远 False |
| 2 | 跳过交付物 = 绕过整个 lifecycle 阶段 | `gate_satisfied` 只看交付物清单，不校验 stage 是否真跑过 |
| 3 | confirm 闸 / story_state 闸 pause 时 PTY 不释放 | `sm_pause` + `return` 前没有 `clean_exit_pty`，PTY 成孤儿 |
| 4 | skip_stage 不生效 | 旧 PTY poll 循环没退出（PTY 活着），driver claim 没释放 |
| 5 | 用户看不到 stage 这一轮干了什么 | TerminalTab 只显示规划时的 `focus`（任务描述），无完成摘要 |
| 6 | lifecycle 只能逐格推进 | 硬编码 `_stages_done` + `gate_satisfied` 一次只查一个状态转换 |

---

## 三个改动点

### 改动 1：一次 LLM 调用做三个决定（合并 boundary_judge + lifecycle 推进 + 摘要）

### 改动 2：stage 退出时 PTY 必须释放（消除孤儿线程）

### 改动 3：UI 显示每轮 stage 摘要

---

## 改动 1 详细设计：一次 LLM 三个决定

### 1.1 现状（要替换的）

stage 完成后，planner.py 最多做三次判断：

```
① boundary_judge（planner.py:1945）—— LLM 判 stage 质量 → approve/reject/escalate
② _stages_done（planner.py:2110）—— 硬编码查 lifecycle 状态的 stages 是否全 _completed
③ gate_satisfied（planner.py:2125）—— 硬编码查交付物清单是否满足
```

②③ 是硬编码的，导致问题 1/2/6。

### 1.2 目标

**一次 LLM 调用，输出三个决定**：

```python
class StageCompletionDecision(BaseModel):
    """stage 完成后一次 LLM 裁判的输出（替换 boundary_judge + _stages_done + gate_satisfied）。"""

    # 决定 1：stage 质量（原 boundary_judge 的职责）
    quality: Literal["approve", "reject", "escalate"]
    # - approve：成果物合格
    # - reject：成果物有缺陷，回 code CLI 重做
    # - escalate：reject 超限/理由重复/LLM 判需人 → paused 等人

    # 决定 2：lifecycle 应该到哪个状态（可跨多状态，替换 _stages_done + gate_satisfied）
    lifecycle_target: Optional[str] = None
    # - None / 空串：不推进，当前 lifecycle 状态的目标还没达成
    # - "开发" / "测试" / "上线" / "结项"：story 现在应该处于这个状态
    #
    # 关键：这不是「推进一格」，而是「最远能到哪」。LLM 看的是当前所有累积产出
    # （不止本轮 stage），判断 story 满足了哪些 lifecycle gate。
    # 例：single-pass 的 verify 跑完产出 spec+code+test_report+delivery →
    #     lifecycle_target="结项"（从开发一口气跨到结项）。

    # 决定 3：本轮摘要（给 UI 展示，新增）
    summary: str = ""
    # 简要说明这一轮 stage 干了什么（1-3 句话）。
    # 例："调研了 BorrowServiceImpl 的提现校验逻辑，分析了提额券数据模型，
    #      确定了前端额度组合判断方案。涉及 5 个文件。"

    # 辅助字段
    reason: str = ""           # quality 判断的理由
    findings: list[dict] = []  # 质量问题（原 boundary_judge 的 findings）
    repair_action: Optional[dict] = None  # reject 时的修复方案（原 boundary_judge 的）
```

### 1.3 LLM 输入

```
你是 story-lifecycle 的 stage 完成裁判。stage 刚跑完，基于产出做三个决定。

## Story 信息
- Story: {story_key}
- task_type: {task_type}
- 当前 lifecycle 状态: {current_lifecycle_state}

## Lifecycle 状态定义（source profile）
{story_states_yaml}
# 例：
# 开发: stages=[design, build], next=测试, confirm=ui_button
# 测试: stages=[verify], next=上线, confirm=config(auto_advance_test)
# 上线: stages=[], next=结项, confirm=none
# 结项: stages=[], next=null

## Lifecycle gate 定义
{lifecycle_gates}
# 例：
# 待启动→开发: [prd, spec]
# 开发→测试: [code]
# 测试→上线: [test_report]
# 上线→结项: [delivery]

## 当前 stage
- stage: {stage}
- adapter: {adapter}
- 修复轮次: {retry_count}/{max_retries}

## 本轮 stage 产出
- 摘要: {done_summary}
- 变更文件: {files_changed}
- 成果物: {artifacts_landed}

## 累积产出（所有已完成的 stage）
{cumulative_outputs}
# 例：
# design(已完成): spec.md — 提现门槛方案设计
# build(刚完成): git diff — 10 个文件（BorrowServiceImpl 等）

## 历史经验
{playbook}

## 你的三个决定

1. **quality**: approve / reject / escalate
   - approve：成果物合格，可以继续
   - reject：成果物有缺陷，需要回 code CLI 重做（附 repair_action）
   - escalate：质量问题超限/没救了 → 转人

2. **lifecycle_target**: story 现在应该处于哪个 lifecycle 状态？
   - 看累积产出，判断满足了哪些 gate
   - 可能跨多个状态：single-pass 的 verify 一次产出所有东西 → target=结项
   - 如果当前状态的目标还没达成 → target=None
   - 注意：你判的是「最远能到哪」，planner 会逐个状态推进，遇到 ui_button 的 confirm 会停住等人确认

3. **summary**: 本轮 stage 干了什么？（1-3 句话，给用户看）

## 纪律
- quality=reject 时，lifecycle_target 应为 None（成果物不合格不算产出）
- 有 HIGH finding 未解决时，倾向 reject/escalate
- 历史 playbook 显示「换 adapter 成功」时，repair_action 用 swap_approach

输出 JSON：
```json
{
  "quality": "approve|reject|escalate",
  "lifecycle_target": "开发|测试|上线|结项|null",
  "summary": "本轮干了什么...",
  "reason": "判断理由",
  "findings": [{"severity":"...","category":"...","description":"..."}],
  "repair_action": {"kind":"retry|swap_approach|insert_rescue_stage|escalate","reason":"...","new_adapter":"...","rescue_stage":"..."}
}
```

### 1.4 planner 怎么用这个输出

替换 planner.py:1945-2218 的整段（boundary_judge + _stages_done + gate_satisfied + story_state_gate），改成：

```python
# stage 完成（_artifacts_ready() True），一次 LLM 裁判
from ..evaluation.stage_completion import judge_stage_completion

_decision = judge_stage_completion(
    story_key=story_key,
    stage=stage,
    workspace=workspace,
    ctx=ctx,
    lifecycle_state=lifecycle_state,
    done_data=done_data,
    cumulative_outputs=_collect_cumulative_outputs(story_key, actions),
    adapter=adapter_name,
    retry_count=ctx.get("_verify_round", 1),
)

# 存摘要（给 UI，改动 3）
_store_stage_summary(story_key, stage, _decision.summary)

# 处理 quality
if _decision.quality == "reject":
    # 插 retry action（同现有 boundary_judge reject 路径）
    ...
    # ★ PTY 释放（改动 2，见下文）
    _release_pty(...)
    break

if _decision.quality == "escalate":
    sm_pause(story_key, ...)
    # ★ PTY 释放
    _release_pty(...)
    return

# quality == "approve" → 处理 lifecycle 推进
if _decision.lifecycle_target and _decision.lifecycle_target != lifecycle_state:
    # 逐个状态推进到 target，遇到 ui_button 停住
    _advanced = _advance_lifecycle_to_target(
        story_key=story_key,
        ctx=ctx,
        current=lifecycle_state,
        target=_decision.lifecycle_target,
        story_states=story_states,
    )
    if _advanced.paused_for_confirm:
        # 遇到 ui_button，paused 通知用户
        # ★ PTY 释放
        _release_pty(...)
        return
    # 全程自动推进（confirm=none/config），lifecycle 已到 target
    lifecycle_state = _advanced.new_state

# stage 间 confirm 闸（保留，但简化）
if stage_cfg.confirm and stage != "verify":
    ctx["_stage_gate"] = {...}
    sm_pause(story_key, ctx_updates=ctx)
    # ★ PTY 释放
    _release_pty(...)
    return

# 无 confirm 闸或已确认 → continue 外层 while，跑 next stage
```

### 1.5 lifecycle_target 的推进逻辑

`_advance_lifecycle_to_target` 逐个状态推进，**遇到 ui_button 停住**：

```python
def _advance_lifecycle_to_target(*, story_key, ctx, current, target, story_states):
    """从 current 逐个状态推进到 target，遇到 confirm=ui_button 停住等人。

    LLM 已判断 target 可达（看了所有累积产出），所以这里不重新检查 gate_satisfied。
    只按 confirm 规则决定自动转还是停住。
    """
    order = ["待启动", "开发", "测试", "上线", "结项"]
    cur_idx = order.index(current)
    tgt_idx = order.index(target)

    paused = False
    for i in range(cur_idx, tgt_idx):
        from_state = order[i]
        to_state = order[i + 1]
        state_def = story_states.get(from_state, {})
        confirm = (state_def.get("confirm") or {})
        ctype = confirm.get("type", "none")

        if ctype == "ui_button":
            # 停住等人确认
            ctx["_story_state_gate"] = {
                "from": from_state,
                "to": to_state,
                "awaiting_confirm": True,
                "label": confirm.get("label", f"进入{to_state}"),
                # 记住最终 target，用户确认后继续推进
                "final_target": target,
            }
            sm_pause(story_key, ctx_updates=ctx)
            db.log_event(story_key, "", "story_state_gate_reached",
                         {"from": from_state, "to": to_state, "final_target": target})
            paused = True
            break  # 停住，不继续推进
        else:
            # none / config → 自动推进
            ctx["_lifecycle_state"] = to_state
            db.update_story(story_key, lifecycle_state=to_state,
                            context_json=json.dumps(ctx, ensure_ascii=False))
            db.log_event(story_key, "", "story_state_transition",
                         {"from": from_state, "to": to_state, "auto": True})

    return type("AdvResult", (), {
        "new_state": ctx.get("_lifecycle_state", current),
        "paused_for_confirm": paused,
    })()
```

**用户确认后继续推进**：`/lifecycle/advance` 收到确认后，检查 `ctx["_story_state_gate"]["final_target"]`，如果还没到 target，继续推进（走同样的循环）。

### 1.6 新文件

创建 `packages/story-lifecycle/src/story_lifecycle/orchestrator/evaluation/stage_completion.py`：

- `class StageCompletionDecision(BaseModel)` — 输出结构
- `def judge_stage_completion(*, story_key, stage, workspace, ctx, lifecycle_state, done_data, cumulative_outputs, adapter, retry_count) -> dict` — 主函数
- `def _build_prompt(...) -> str` — 组装 LLM prompt
- `def _collect_cumulative_outputs(story_key, actions) -> str` — 收集所有已完成 stage 的产出摘要
- `def _fallback_decision(...) -> dict` — LLM 不可用时的降级（quality=approve, lifecycle_target=None, summary=""）

LLM 调用方式参考 `boundary_judge.py:82-103`（`get_llm()` + structured output）。

### 1.7 删除的代码

- planner.py:2099-2218 的 `_stages_done` + `gate_satisfied` + story_state_gate 整段
- planner.py:1945-2004 的 `boundary_judge` 调用（被 `judge_stage_completion` 替代）
- `unified_gate.py` 的 verify 质量判断可以合并进 `judge_stage_completion`（verify stage 完成时一次判），或暂时保留（向后兼容）—— **建议先保留 unified_gate，只替换 boundary_judge + lifecycle 部分**，减小改动面

---

## 改动 2 详细设计：PTY 释放

### 2.1 问题

planner.py 的 launch 分支（1060-2266）有多个退出路径，部分不释放 PTY：

| 退出路径 | 行号 | PTY 释放 |
|---|---|---|
| CLI launch 失败 | 1539-1540 | ✓（PTY 没建） |
| headless 重试耗尽 | 1654-1658 | ✓ |
| headless 死了没产出 | 1675-1682 | ✓ |
| PTY 死了没产出 | 1690-1710 | 部分（PTY 已死） |
| stuck restart break | 1846 | ✓（_agent_pty.kill()） |
| boundary reject break | 1990-1999 | ✓（clean_exit + kill） |
| **story_state_gate paused** | **2175-2188** | **✗ 孤儿！** |
| **stage_confirm_gate paused** | **2249-2265** | **✗ 孤儿！** |
| stage 完成 break | 2025-2096 | ✓（clean_exit） |
| 超时 | 2273-2280 | 部分 |

### 2.2 修复：try/finally 保证 PTY 释放

在 launch 分支（planner.py:1060 附近 `_agent_pty = None` 之后）包 try/finally：

```python
# planner.py launch 分支（stage action 处理）
_agent_pty = None
headless_proc = None

try:
    # ---- spawn ----
    # ...（现有 spawn 逻辑，~1060-1540）

    # ---- poll 循环 ----
    # ...（现有 poll while，~1611-2266）
    #   所有 return / break / sm_pause 都在 try 块内
    #   不需要每处单独释放 PTY —— finally 兜底

except Exception:
    log.exception("[%s] stage %s unexpected error", story_key, stage)
    sm_mark_failed(story_key, f"Stage {stage}: unexpected error")
    raise  # 或 return，取决于外层异常处理

finally:
    # ★ 无论怎么退出（return/break/异常/pause），PTY 都释放
    if headless_proc is not None:
        _kill_headless(headless_proc)
    if _agent_pty is not None:
        try:
            from ...infra.terminal.pty import clean_exit_pty, kill_pty
            # sid 捕获（现有逻辑移到这里，集中管理）
            if _need_sid_capture:
                _capturer = adapter.make_sid_capturer(
                    story_key, stage, cwd=_spawn_cwd, since_ts=_spawn_ts)
                clean_exit_pty(_agent_pty, on_output=_capturer)
                _captured = adapter.capture_sid_post_exit(
                    story_key, stage, cwd=_spawn_cwd, since_ts=_spawn_ts)
                if _captured:
                    from ...infra.db import models as _sd
                    _sd.set_session_id(story_key, stage, adapter_name, _captured)
            else:
                clean_exit_pty(_agent_pty)
        except Exception as exc:
            log.warning("[%s] clean_exit_pty failed for stage %s: %s; force-killing",
                        story_key, stage, exc)
            try:
                _agent_pty.kill()
            except Exception:
                pass
        finally:
            # 从 PTY 注册表移除（kill_pty 做这个）
            try:
                from ...infra.terminal.pty import kill_pty
                kill_pty(story_key, _agent_pty.session_id)
            except Exception:
                pass
            _agent_pty = None
```

### 2.3 关键注意点

1. **sid 捕获移到 finally**：原来 sid 捕获在 stage 完成的正常路径（2031-2069），现在统一移到 finally。无论怎么退出都尝试捕获 sid，保证 resume 可用。

2. **session_id 已持久化**：PTY 释放后 claude 进程会死，但 `story_session` 表里的 session_id 还在（spawn 时就写入了）。resume 时用 `--resume <sid>` 恢复（planner.py:1204-1221 已有此逻辑）。

3. **删除现有散落的 PTY 释放代码**：1990-1996（reject 路径）和 2025-2096（完成路径）的 `clean_exit_pty` + `kill_pty` 可以删除——finally 统一处理。保留它们会导致 double-kill（虽然 kill_pty 是幂等的，但代码冗余）。

4. **poll 循环里的 return/break 不需要改**：它们只需要正常 return/break，finally 会处理 PTY。现有代码里 return 前的 `clean_exit_pty` 调用可以删掉（finally 兜底），但保留也无害（幂等）。

5. **sm_pause 后的 return**：改动 1 的 `lifecycle_target` 推进遇到 ui_button 时 `sm_pause` + `return`，PTY 由 finally 释放。用户确认后 `/lifecycle/advance` → `start_story_async` → 重新 spawn（用 session_id resume）。

---

## 改动 3 详细设计：UI 显示 stage 摘要

### 3.1 后端：存 summary

`judge_stage_completion` 输出的 `summary` 存到 `story_session` 表（已有 `outcome` 字段可复用，或新增 `completion_summary` 字段）：

**方案 A（推荐，零迁移）**：复用 `story_session.outcome` 字段存 summary。
```python
# judge_stage_completion 返回后
db.upsert_session(story_key, stage, adapter_name,
                  session_id=..., status="completed")
# outcome 字段单独更新
db.set_session_outcome(story_key, stage, adapter_name,
                       outcome="completed",
                       completion_summary=_decision.summary)
```

需要在 models.py 加一个 helper：
```python
def set_session_outcome(story_key, stage, adapter, outcome, completion_summary=None):
    """更新 session 的 outcome + completion_summary。"""
    with _db() as conn:
        conn.execute(
            "UPDATE story_session SET outcome=?, updated_at=? "
            "WHERE story_key=? AND stage=? AND adapter=?",
            (outcome, _now_iso(), story_key, stage, adapter)
        )
        # completion_summary 用 artifacts_prod 字段存（JSON），或新增列
```

**方案 B（干净但需迁移）**：新增 `completion_summary TEXT` 列到 story_session 表（幂等 ALTER TABLE，已有先例 models.py:620）。

### 3.2 后端：API 暴露

`GET /api/story/{story_key}/sessions`（已有，api.py 的 `list_sessions_for_story`）返回 session 行，确保包含 `completion_summary`（或 `outcome`）字段。

### 3.3 前端：TerminalTab 展示

在 `TerminalTab.tsx` 的 stage 面板（`📋 {focus}` 行下方）加完成摘要区域：

```tsx
// TerminalTab.tsx，278 行下方
{focus && <div className="tt-stage-focus">📋 {focus}</div>}

{/* 新增：stage 完成摘要 */}
{stageSession?.completion_summary && isDone && (
  <div className="tt-stage-summary">
    <span className="tt-stage-summary-icon">✅</span>
    <span className="tt-stage-summary-text">{stageSession.completion_summary}</span>
  </div>
)}
```

需要从 sessions 数据里找到当前 stage 的 session：
```tsx
// TerminalTab.tsx 已有 sessions 数据（fetchSessions 拉）
const stageSession = sessions.find(s => s.stage === sel)
const isDone = stageSession?.status === 'completed' || stageSession?.outcome === 'completed'
```

CSS（加到 TerminalTab 的样式文件）：
```css
.tt-stage-summary {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  margin: 8px 0;
  background: rgba(34, 197, 94, 0.08);
  border-left: 3px solid #22c55e;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.5;
  color: #d1d5db;
}
.tt-stage-summary-icon {
  flex-shrink: 0;
}
```

### 3.4 效果

用户看到的 stage 面板：

```
design ✓
🟠 claude
─────────────────────
📋 调研提现门槛现有逻辑，分析提额券数据模型，设计改动方案

✅ 本轮完成：调研了 BorrowServiceImpl 的提现校验逻辑，分析了
   提额券数据模型，确定了前端额度组合判断方案。涉及 5 个文件。
─────────────────────
```

---

## 实施顺序

1. **改动 2（PTY 释放）** —— 先做，最独立，立即解决孤儿线程
2. **改动 1（一次 LLM 三个决定）** —— 核心，替换硬编码判据
3. **改动 3（UI 摘要）** —— 最后做，依赖改动 1 的输出

---

## 测试验证

### 改动 2 验证
- 跑 minimal profile 的 design stage，confirm 闸 pause 后检查 PTY 进程是否释放（`tasklist | grep claude`）
- 跑到 story_state_gate pause，同样检查
- skip_stage 后检查旧 PTY 是否释放

### 改动 1 验证
- **single-pass 场景**：verify 跑完产出所有东西 → `lifecycle_target` 应为 "结项" 或至少 "上线"（而非卡在开发）
- **minimal 场景**：design 跑完 → `lifecycle_target=None`（还差 code）；build 跑完 → `lifecycle_target="测试"`
- **跳过交付物不再绕过执行**：LLM 看实际产出，跳过交付物不影响判断（LLM 不依赖 `_skipped_deliverables`）

### 改动 3 验证
- stage 完成后 TerminalTab 显示 `✅ 本轮完成：...`
- 切换 stage tab 能看到各自的 summary

---

## 关键文件清单

| 文件 | 改动 |
|------|------|
| `orchestrator/evaluation/stage_completion.py` | **新建**：StageCompletionDecision + judge_stage_completion |
| `orchestrator/engine/planner.py:1060-2266` | launch 分包 try/finally 释放 PTY；替换 boundary_judge + _stages_done + gate_satisfied 为 judge_stage_completion |
| `orchestrator/evaluation/boundary_judge.py` | 可保留（向后兼容）或废弃（功能并入 stage_completion） |
| `sourcing/deliverables.py:gate_satisfied` | planner 不再调用（保留给 /lifecycle/advance 手动推进用） |
| `infra/db/models.py` | `set_session_outcome` helper（存 completion_summary） |
| `frontend/src/components/TerminalTab.tsx:278` | 加 `✅ 本轮完成` 展示区域 |

---

## 不改的部分

- `sourcing/deliverables.py` 的 `check_deliverables` / `gate_for_current_state` / `gate_satisfied` —— 保留，`/lifecycle/advance`（用户手动推进）仍用交付物 gate
- `unified_gate.py` —— 保留，verify stage 的质量门 + 外部 provider 集成仍走它
- `story_session` 表结构 —— 不改（用 outcome 字段或幂等加列）
- profile yaml（minimal/single-pass/...）—— 不改
- source yaml（tapd/default）—— 不改
