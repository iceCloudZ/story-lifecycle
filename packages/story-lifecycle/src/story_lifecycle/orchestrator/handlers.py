"""DecisionHandler 子类（设计 13 Step 4）— judge 决策三分支副作用。

从 planner.py driver 的决策段收敛而来：
- approve: 写 _completed_stages + summary + lifecycle 推进（advance_lifecycle_to_target）
  + 找下一个 stage + 全 stage 完成 → completed
- reject: 半自动 → pause；全自动 → 插 retry action
- escalate: pause 等人

半自动（Interactive）与全自动（Automatic）的区别只在 reject 分支：
半自动 reject 转人（pause + 前端显示原因），全自动 reject 插 retry 自动重做。
"""

from __future__ import annotations

import json
import logging

from .abc import DecisionHandler

log = logging.getLogger("story-lifecycle.handlers")


def _load_story_states(story: dict) -> dict:
    """从 source profile 拿 story_states（给 advance_lifecycle_to_target 用）。"""
    try:
        from ..sourcing.source_loader import resolve_source_profile

        return resolve_source_profile(story.get("source_type")).story_states or {}
    except Exception:
        return {}


class BaseDecisionHandler(DecisionHandler):
    """共享实现：approve 的完成登记 + lifecycle 推进 + 下 stage 选择。

    子类差异：``handle_reject``（半自动 pause vs 全自动插 retry）。
    """

    #: approve 后是否把下一 stage 标记为 current_stage（子类可覆写）
    AUTO_ADVANCE = False

    def _persist_ctx(self, story_key: str, ctx: dict) -> None:
        from ..infra.db import models as db

        db.update_story(
            story_key, context_json=json.dumps(ctx, ensure_ascii=False)
        )

    def _save_summary(self, story_key: str, stage: str, adapter: str, summary: str) -> None:
        from ..infra.db import models as db

        if not summary:
            return
        try:
            db.set_session_completion_summary(story_key, stage, adapter, summary)
        except Exception:
            pass

    def handle_approve(
        self,
        story_key: str,
        stage: str,
        decision: dict,
        ctx: dict,
        actions: list,
    ) -> bool:
        """approve: 写 _completed_stages + lifecycle 推进 + summary。

        Returns: True 若 paused_for_confirm（调用方停止推进下一 stage）。
        """
        from ..infra.db import models as db

        story = db.get_story(story_key) or {}
        adapter = ""
        for a in actions or []:
            if a.get("stage") == stage:
                adapter = a.get("adapter", "")
                break

        # 记进度:追加 current stage 到 _completed_stages 并持久化
        completed = list(ctx.get("_completed_stages") or [])
        if stage not in completed:
            completed.append(stage)
        ctx["_completed_stages"] = completed
        self._persist_ctx(story_key, ctx)

        # 会话恢复回填 + summary（TerminalTab 展示）
        try:
            db.complete_session(story_key, stage, adapter)
        except Exception:
            pass
        self._save_summary(story_key, stage, adapter, decision.get("summary", ""))

        # lifecycle 推进(LLM 判的 lifecycle_target,遇 ui_button 停住)
        lifecycle_state = ctx.get("_lifecycle_state") or story.get("lifecycle_state") or "待启动"
        target = decision.get("lifecycle_target")
        if target and target != lifecycle_state:
            from .evaluation.stage_completion import advance_lifecycle_to_target

            advanced = advance_lifecycle_to_target(
                story_key=story_key,
                ctx=ctx,
                current=lifecycle_state,
                target=target,
                story_states=_load_story_states(story),
            )
            if advanced.get("paused_for_confirm"):
                log.info(
                    "[%s] approve %s: lifecycle target=%s paused for confirm",
                    story_key,
                    stage,
                    target,
                )
                return True
            lifecycle_state = advanced.get("new_state", lifecycle_state)

        # 阶段间闸(PLAN-stage-confirm-gate):stage_cfg.confirm=True 且后面还有未完成
        # launch action → paused。verify 是最后阶段无下一 stage,不受影响。
        if self._maybe_stage_gate(story_key, stage, ctx, actions):
            return True

        # 找下一个 stage（AUTO_ADVANCE 才推进 current_stage;否则留给编排线程按 actions 推）
        next_stage = self._find_next_stage(actions, completed)
        if next_stage and self.AUTO_ADVANCE:
            ctx["current_stage"] = next_stage
            self._persist_ctx(story_key, ctx)
            try:
                db.update_story(story_key, current_stage=next_stage)
            except Exception:
                pass
            log.info("[%s] approve %s → next stage %s", story_key, stage, next_stage)
        elif not next_stage:
            # 所有 launch stage 完成 → completed
            self._handle_all_stages_done(story_key, ctx, actions)
        return False

    def _maybe_stage_gate(self, story_key: str, stage: str, ctx: dict, actions: list) -> bool:
        """阶段间确认闸（与 driver 同逻辑）。返回 True 若 paused。"""
        from ..infra.db import models as db
        from ..sourcing.state_machine import pause as sm_pause
        from .engine.profile_loader import resolve_profile

        story = db.get_story(story_key) or {}
        try:
            rp = resolve_profile(story.get("profile") or "minimal")
            profile_stages = {n: c for n, c in rp.stages.items()}
        except Exception:
            profile_stages = {}
        stage_cfg = profile_stages.get(stage)
        confirm_on = bool(
            stage_cfg
            and getattr(stage_cfg, "confirm", False)
            and stage != "verify"
        )
        if not confirm_on:
            return False
        completed = list(ctx.get("_completed_stages") or [])
        next_stage = self._find_next_stage(actions, completed)
        if next_stage is None:
            return False
        ctx["_stage_gate"] = {
            "completed_stage": stage,
            "next_stage": next_stage,
            "awaiting_confirm": True,
        }
        sm_pause(story_key, ctx_updates=ctx)
        try:
            db.log_event(
                story_key,
                stage,
                "stage_gate_reached",
                {"completed_stage": stage, "next_stage": next_stage},
            )
        except Exception:
            pass
        log.info(
            "[%s] stage gate: %s done → paused awaiting confirm to advance to %s",
            story_key,
            stage,
            next_stage,
        )
        return True

    def _find_next_stage(self, actions: list, completed: list) -> str | None:
        """找第一个未完成的 launch action stage。"""
        for a in actions or []:
            if a.get("action") != "launch":
                continue
            st = a.get("stage")
            if st and st not in completed:
                return st
        return None

    def _handle_all_stages_done(self, story_key: str, ctx: dict, actions: list) -> None:
        """所有 launch action 完成 → story completed + 复盘/飞轮回写。"""
        from ..infra.db import models as db
        from ..sourcing.state_machine import mark_completed as sm_mark_completed
        from .engine.planner import _persist_playbook_for_story, _write_retrospect

        story = db.get_story(story_key) or {}
        workspace = story.get("workspace", "")
        sm_mark_completed(story_key, ctx_updates=ctx)
        log.info("[%s] All stages completed", story_key)
        try:
            _write_retrospect(workspace, story_key, actions)
        except Exception:
            log.exception("[%s] retrospect write failed", story_key)
        try:
            _persist_playbook_for_story(workspace, story_key, db)
        except Exception:
            log.exception("[%s] playbook persist failed", story_key)


class InteractiveDecisionHandler(BaseDecisionHandler):
    """半自动：reject → pause 等人（前端显示 reject 原因，用户手动重做）。"""

    def handle_reject(
        self,
        story_key: str,
        stage: str,
        decision: dict,
        ctx: dict,
        actions: list,
    ) -> None:
        from ..infra.db import models as db
        from ..sourcing.state_machine import pause as sm_pause

        reason = decision.get("reason", "stage completion reject")
        sm_pause(story_key, ctx_updates=ctx)
        try:
            db.log_event(
                story_key,
                stage,
                "stage_completion_rejected",
                {"reason": reason},
            )
        except Exception:
            pass
        log.info("[%s] interactive reject %s: %s → paused", story_key, stage, reason[:120])

    def handle_escalate(self, story_key: str, stage: str, decision: dict, ctx: dict) -> None:
        from ..infra.db import models as db
        from ..sourcing.state_machine import pause as sm_pause

        reason = decision.get("reason", "stage completion escalate")
        sm_pause(story_key, ctx_updates=ctx)
        try:
            db.log_event(
                story_key,
                stage,
                "stage_completion_escalated",
                {"reason": reason},
            )
        except Exception:
            pass
        log.info("[%s] escalate %s: %s → paused", story_key, stage, reason[:120])


class AutomaticDecisionHandler(BaseDecisionHandler):
    """全自动：reject → 插 retry action（下一轮编排线程 spawn 重做）。

    stage 间确认闸（_stage_gate）仍生效 —— 设计 13 API 表「PUT /advance →
    清 confirm 标记（编排线程下一轮继续）」依赖它；全自动只豁免「等人点
    启动 CLI」的 spawn 责任，不豁免 profile 声明的确认闸。
    """

    AUTO_ADVANCE = True

    def handle_reject(
        self,
        story_key: str,
        stage: str,
        decision: dict,
        ctx: dict,
        actions: list,
    ) -> None:
        from ..infra.db import models as db
        from ..infra.paths import stage_done_file_rel

        reason = decision.get("reason", "stage completion reject")
        retry = {
            "action": "launch",
            "stage": stage,
            "adapter": "",
            "focus": f"上轮 stage 完成裁判 reject:{reason}",
            "done_file": stage_done_file_rel(story_key, stage),
        }
        # 找当前 stage 的 adapter（沿用原 action 的）
        for a in actions or []:
            if a.get("stage") == stage:
                retry["adapter"] = a.get("adapter", "")
                break
        # 插到当前 stage 之后（对齐 driver：idx+1 插入）
        insert_at = len(actions)
        for i, a in enumerate(actions or []):
            if a.get("stage") == stage:
                insert_at = i + 1
        actions.insert(insert_at, retry)
        ctx["_agent_actions"] = actions
        try:
            db.update_story(
                story_key, context_json=json.dumps(ctx, ensure_ascii=False)
            )
            db.log_event(
                story_key,
                stage,
                "boundary_reject_retry",
                {"reason": reason, "next_stage": stage},
            )
        except Exception:
            log.exception("[%s] reject retry persist failed", story_key)
        log.info("[%s] auto reject %s → retry inserted: %s", story_key, stage, reason[:80])

    def handle_escalate(self, story_key: str, stage: str, decision: dict, ctx: dict) -> None:
        from ..infra.db import models as db
        from ..sourcing.state_machine import pause as sm_pause

        reason = decision.get("reason", "stage completion escalate")
        sm_pause(story_key, ctx_updates=ctx)
        try:
            db.log_event(
                story_key,
                stage,
                "stage_completion_escalated",
                {"reason": reason},
            )
        except Exception:
            pass
        log.info("[%s] escalate %s: %s → paused", story_key, stage, reason[:120])


def make_decision_handler(story: dict, ctx: dict) -> DecisionHandler:
    """Factory：按 profile 创建 DecisionHandler（与 make_stage_executor 同源）。"""
    from .executors import make_stage_executor

    executor = make_stage_executor(story, ctx)
    from .executors import AutomaticStageExecutor

    if isinstance(executor, AutomaticStageExecutor):
        return AutomaticDecisionHandler()
    return InteractiveDecisionHandler()
