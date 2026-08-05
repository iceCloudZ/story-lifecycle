"""OrchestratorThread（设计 13 Step 5）— 全局编排线程。

serve 启动时起、serve 停时止的一个 daemon 线程，负责**所有 story 的所有 PTY 的
生命周期管理**。替代三套旧机制：
- driver（continue_orchestrator_agent 的 poll 循环）
- orphan 认领（consume_orphan_artifacts / GET /story 副作用）
- async watcher（_watch_interactive_done_files）

循环：每 poll_interval 秒一轮，遍历所有 active story：
  有 PTY 活着 → poll artifacts；落地 → submit judge（子线程，不阻塞主循环）
  有 PTY 死了 → artifacts 落地则 judge，没落地则 pause
  无 PTY → executor.maybe_spawn（全自动 spawn / 半自动等人点）
  judge 结果就绪 → handler 处理（approve/reject/escalate 三分支）

judge 放 ThreadPoolExecutor 子线程（调 LLM ~10-30s），结果写回内存
``_judge_results``，主循环下一轮读。编排线程是**单写者**：DB 读写不需要锁；
HTTP 请求线程只改标记（ctx/status），编排线程读标记执行。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..infra.db import models as db
from ..sourcing.state_machine import pause as sm_pause
from .executors import find_action, load_ctx, make_stage_executor
from .handlers import make_decision_handler

log = logging.getLogger("story-lifecycle.scheduler")


def _story_states(story: dict) -> dict:
    """从 source profile 拿 story_states（给 judge 判 lifecycle_target 用）。"""
    try:
        from ..sourcing.source_loader import resolve_source_profile

        return resolve_source_profile(story.get("source_type")).story_states or {}
    except Exception:
        return {}


def list_active_for_orchestrator() -> list[str]:
    """编排线程要驱动的 story key 列表（status=active 且 intake_state=ready）。

    设计13 替代 watcher 的 find_ready_interactive_stories：不做 done 文件预筛
    （编排线程自己 poll artifacts），只排除 paused（等人介入）的 story。
    """
    return [
        s["story_key"]
        for s in db.list_active_stories()
        if s.get("status") == "active" and s.get("intake_state") == "ready"
    ]


def _read_done_data(story_key: str, stage: str, workspace: str, actions: list) -> dict:
    """读 done.json 兼容视图作 judge payload；没有则合成（与 driver 同口径）。"""
    from ..infra.json_helpers import robust_json_parse
    from ..infra.paths import stage_done_file_rel

    done_rel = stage_done_file_rel(story_key, stage)
    done_path = workspace and f"{workspace}/{done_rel}"
    try:
        from pathlib import Path

        p = Path(done_path) if done_path else None
        if p and p.exists():
            return robust_json_parse(p) or {}
    except Exception:
        pass
    return {
        "stage": stage,
        "status": "done",
        "summary": f"{stage} 成果物落地",
        "files_changed": [],
    }


class OrchestratorThread(threading.Thread):
    """全局编排线程（daemon）。一个实例，serve 启动时起，serve 停时止。"""

    #: 单 stage 超时（对齐 driver poll_timeout 45min；env STORY_STAGE_TIMEOUT 可覆盖，测试用）
    STAGE_TIMEOUT = float(os.environ.get("STORY_STAGE_TIMEOUT", "2700"))

    def __init__(
        self,
        poll_interval: float = 5.0,
        max_judge_workers: int = 4,
        force_auto: bool = False,
    ):
        super().__init__(daemon=True, name="orchestrator")
        self._poll_interval = poll_interval
        self._executor_pool = ThreadPoolExecutor(max_workers=max_judge_workers)
        self._judging: set[str] = set()  # 正在 judge 的 story_key:stage（防重复）
        self._judge_results: dict[str, dict] = {}  # story_key:stage → decision
        # story_key:stage → done_data：_submit_judge 存，approve 后 _finalize_stage_pass
        # 取用（log_event completed / register / commit）。judge 失败兜底 approve 时
        # 取不到 → _finalize_stage_pass 用空 done_data 兜底（非阻塞）。
        self._stage_done_data: dict[str, dict] = {}
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 每个 story 当前 stage 的运行时状态（spawn 时刻 / 卡住去重 / headless 重试）
        self._stage_state: dict[str, dict] = {}
        # 缓存每 story 的 executor 实例（跨 tick 保留 _last_pty 等实例态；
        # 换 stage / 换 profile 时重建）。
        self._executors: dict[str, object] = {}
        # force_auto:旧 driver 语义（continue_orchestrator_agent 同步入口总是自动
        # spawn，不按 profile 分半自动/全自动）；serve 编排线程用 profile 分。
        self._force_auto = force_auto

    # ---- 生命周期 ----

    def stop(self):
        """通知线程停止（serve 停时调）。"""
        self._stop_event.set()

    def run(self):
        log.info("orchestrator thread started (poll=%ss)", self._poll_interval)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("orchestrator tick failed (non-fatal, continuing)")
            self._stop_event.wait(self._poll_interval)
        log.info("orchestrator thread stopped")

    # ---- 一轮轮询 ----

    def _tick(self):
        """一轮轮询：遍历所有 active story。"""
        for story in db.list_active_stories():
            if story.get("intake_state") != "ready":
                continue
            if story.get("status") != "active":
                # paused（plan confirm / stage gate / escalate）等人介入，编排线程不管
                continue
            story_key = story["story_key"]
            try:
                self._tick_story(story)
            except Exception:
                log.exception(
                    "[%s] orchestrator tick story failed (non-fatal)", story_key
                )

    def _tick_story(self, story: dict):
        story_key = story["story_key"]
        ctx = load_ctx(story)
        stage = story.get("current_stage", "")
        if not stage:
            return
        actions = ctx.get("_agent_actions") or []
        if not actions:
            return

        executor = self._resolve_executor(story, ctx, stage)
        handler = make_decision_handler(story, ctx)
        if self._force_auto:
            from .executors import AutomaticStageExecutor
            from .handlers import AutomaticDecisionHandler

            if not isinstance(executor, AutomaticStageExecutor):
                executor = AutomaticStageExecutor()
                try:
                    executor._stage_for = stage
                except Exception:
                    pass
                self._executors[story_key] = executor
            handler = AutomaticDecisionHandler()
        judge_key = f"{story_key}:{stage}"

        # 0. current_stage 与 actions 错配/已完成时推进到第一个未完成 launch stage
        #    (对齐 driver 的 start_idx 逻辑:skip 在前 / 老 story current_stage 滞后 /
        #    resume 时已完成 stage 不重跑)。
        action = find_action(actions, stage)
        completed0 = list(ctx.get("_completed_stages") or [])
        if stage in completed0 or action is None or action.get("action") != "launch":
            first = self._next_launch_stage(actions, completed0)
            if first and first != stage:
                db.update_story(story_key, current_stage=first)
                return

        # 0. skip action:记录跳过 + 推进到下一个未完成 launch stage(对齐 driver)。
        action = find_action(actions, stage)
        if action is not None and action.get("action") == "skip":
            reason = action.get("reason", "")
            db.log_event(story_key, stage, "skipped", {"reason": reason})
            log.info("[%s] Skipped stage %s: %s", story_key, stage, reason)
            completed = list(ctx.get("_completed_stages") or [])
            if stage not in completed:
                completed.append(stage)
            ctx["_completed_stages"] = completed
            next_stage = self._next_launch_stage(actions, completed)
            if next_stage:
                db.update_story(
                    story_key,
                    current_stage=next_stage,
                    context_json=json.dumps(ctx, ensure_ascii=False),
                )
            else:
                self._complete_story(story_key, ctx, actions)
            return

        # 0b. 所有 launch stage 已完成 → completed(对齐 driver 末尾 sm_mark_completed)。
        if not self._next_launch_stage(actions, ctx.get("_completed_stages") or []):
            self._complete_story(story_key, ctx, actions)
            return

        # 1. judge 结果就绪 → 处理（子线程写 _judge_results，主循环读）
        result = self._take_judge_result(story_key, stage)
        if result is not None:
            self._handle_decision(
                story_key, stage, result, ctx, actions, handler, executor, story
            )
            return

        # 1b. 正在 judge（结果未就绪）→ 等子线程写（防重复 submit）
        if judge_key in self._judging:
            return

        # 2. PTY 在跑 → poll artifacts + 卡住/超时检测
        pty = executor.get_pty(story_key, stage)
        if pty is not None and getattr(pty, "alive", False):
            self._tick_alive_pty(story_key, stage, pty, executor, story, ctx, actions)
            return

        # 3. PTY 死了 → 看 artifacts（headless 死了没产出 → 重试，对齐 driver）
        if pty is not None:
            if executor.is_artifacts_ready(story_key, stage):
                self._submit_judge(story_key, stage, ctx, executor, story)
                return
            # headless 重试（对齐 driver HEADLESS_MAX_ATTEMPTS）
            if self._maybe_retry_headless(story_key, stage, executor, ctx):
                return
            # PTY 死了没产出 → pause（等人介入）
            log.warning(
                "[%s] PTY died without artifacts for stage=%s → paused",
                story_key,
                stage,
            )
            self._clear_stage_state(story_key)
            sm_pause(story_key, error=f"PTY died without artifacts for {stage}")
            return

        # 4. 没有 PTY → 半自动等人点「启动 CLI」；但用户手动跑完的成果物
        #    （orphan 情形：无 PTY 也无 driver）也要被 judge —— 这替代旧
        #    consume_orphan_artifacts（GET /story 副作用）。全自动 executor
        #    的 maybe_spawn 会自己 spawn，spawn 后下一轮走 PTY 分支。
        if executor.is_artifacts_ready(story_key, stage):
            self._submit_judge(story_key, stage, ctx, executor, story)
            return
        executor.maybe_spawn(story_key, stage, ctx)
        # spawn 后记录 stage 启动时刻（超时判据）
        if executor.get_pty(story_key, stage) is not None:
            self._stage_state[story_key] = {
                "stage": stage,
                "spawned_ts": time.time(),
                "stuck_escalated": False,
            }

    def _tick_alive_pty(
        self,
        story_key: str,
        stage: str,
        pty,
        executor,
        story: dict,
        ctx: dict,
        actions: list,
    ):
        """PTY 活着：成果物落地 → judge；否则卡住检测 + 超时。"""
        # 成果物落地 → judge（先释放 PTY，对齐 driver finally 收口）
        if executor.is_artifacts_ready(story_key, stage):
            self._submit_judge(story_key, stage, ctx, executor, story)
            return
        state = self._stage_state.get(story_key) or {}
        now = time.time()
        spawned_ts = state.get("spawned_ts") or now
        # 超时 → 标 failed（对齐 driver poll_timeout）
        if now - spawned_ts > self.STAGE_TIMEOUT:
            from ..sourcing.state_machine import mark_failed as sm_mark_failed

            log.warning(
                "[%s] stage=%s timed out after %ss",
                story_key,
                stage,
                self.STAGE_TIMEOUT,
            )
            self._clear_stage_state(story_key)
            try:
                sm_mark_failed(story_key, f"Stage {stage} timed out")
            except Exception:
                pass
            return
        # 卡住检测（PTY 路径，对齐 driver STEP 1.7c + STEP 2）
        self._tick_stuck_check(
            story_key, stage, pty, executor, story, ctx, actions, now
        )

    def _tick_stuck_check(
        self, story_key, stage, pty, executor, story, ctx, actions, now
    ):
        """规则卡住检测（零 LLM）+ 命中后的 LLM 诊断（restart/escalate/wait）。

        对齐 driver poll 循环 STEP 1.7c/STEP 2：读 pty_logger events 判卡住，
        detect_stuck → diagnose → 执行决策。_stage_state.stuck_escalated 去重。
        """
        state = self._stage_state.get(story_key) or {}
        if state.get("stuck_escalated"):
            return
        try:
            from ..infra.terminal.pty_logger import read_events as _read_ev

            events = _read_ev(getattr(pty, "log_dir", None) or "", limit=50)
            if not events:
                return
            from .engine.supervisor import detect_stuck

            last_ts = None
            for ev in reversed(events):
                if ev.get("dir") == "output":
                    try:
                        from datetime import datetime as _dt

                        last_ts = _dt.fromisoformat(
                            str(ev.get("ts", "")).replace("Z", "+00:00")
                        ).timestamp()
                    except (ValueError, TypeError):
                        pass
                    break
            det = detect_stuck(
                last_output_ts=last_ts,
                now_ts=now,
                process_alive=getattr(pty, "alive", True),
                events=events,
            )
            if not det:
                return
            # STEP 2：LLM 诊断（summary 优先，规则触发 agentic 例外）
            from .evaluation.stuck_diagnose import (
                diagnose_stuck_agentic,
                diagnose_stuck_summary,
                should_upgrade_agentic,
            )
            from ..infra.db import models as db

            _adapter_attr = getattr(pty, "adapter", "")
            if not isinstance(_adapter_attr, str):
                _adapter_attr = ""
            facts = {"adapter": _adapter_attr, "stage": stage}
            events_path = getattr(pty, "events_path", "") or ""
            if should_upgrade_agentic(story_key, stage, det, events=events):
                diag = diagnose_stuck_agentic(
                    story_key=story_key,
                    stage=stage,
                    detection=det,
                    events_path=events_path,
                    story_facts=facts,
                )
            else:
                diag = diagnose_stuck_summary(
                    story_key=story_key,
                    stage=stage,
                    detection=det,
                    events=events,
                    story_facts=facts,
                )
            action = diag.get("action", "escalate")
            if action == "wait":
                log.info("[%s/%s] stuck diagnose: wait (slow)", story_key, stage)
                state["spawned_ts"] = time.time()  # 重置超时时钟
            elif action == "restart":
                log.info(
                    "[%s/%s] stuck diagnose: restart (seed=%s)",
                    story_key,
                    stage,
                    (diag.get("seed") or "")[:80],
                )
                retry = {
                    "action": "launch",
                    "stage": stage,
                    "adapter": _adapter_attr,
                    "focus": f"卡住诊断 restart:{diag.get('reason', '')};seed:{diag.get('seed', '')}",
                }
                from ..infra.paths import stage_done_file_rel

                retry["done_file"] = stage_done_file_rel(story_key, stage)
                # 插到当前 stage action 之后(对齐 driver idx+1 插入语义)
                insert_at = len(actions)
                for _i, _a in enumerate(actions):
                    if _a.get("stage") == stage:
                        insert_at = _i + 1
                actions.insert(insert_at, retry)
                ctx["_agent_actions"] = actions
                db.update_story(
                    story_key, context_json=json.dumps(ctx, ensure_ascii=False)
                )
                try:
                    pty.kill()
                except Exception:
                    pass
                state["stuck_escalated"] = True
                self._clear_stage_state(story_key)
            else:  # escalate
                from .engine.supervisor import escalate_stuck

                escalate_stuck(
                    story_key=story_key,
                    stage=stage,
                    adapter=getattr(pty, "adapter", ""),
                    detection=det,
                    log_event_fn=db.log_event,
                )
                state["stuck_escalated"] = True
        except Exception:
            log.debug(
                "[%s/%s] stuck check failed (non-fatal)",
                story_key,
                stage,
                exc_info=True,
            )

    def _maybe_retry_headless(self, story_key, stage, executor, ctx) -> bool:
        """headless 进程死了没产出 → 重试（对齐 driver HEADLESS_MAX_ATTEMPTS）。

        Returns True 若已重试（调用方返回，等下一轮再判）。
        """
        from .engine.planner import HEADLESS_MAX_ATTEMPTS
        from .executors import AutomaticStageExecutor, _headless_attempts

        if not isinstance(executor, AutomaticStageExecutor):
            return False
        if not executor.is_headless(story_key, stage):
            return False
        attempts = _headless_attempts.get((story_key, stage), 0)
        if attempts >= HEADLESS_MAX_ATTEMPTS:
            return False
        log.warning(
            "[%s] headless exited without artifacts (attempt %d/%d); re-launching",
            story_key,
            attempts,
            HEADLESS_MAX_ATTEMPTS,
        )
        try:
            action = next(
                (
                    a
                    for a in (ctx.get("_agent_actions") or [])
                    if a.get("stage") == stage
                ),
                {"action": "launch", "stage": stage, "adapter": "", "focus": ""},
            )
            executor.spawn(story_key, stage, action)
            self._stage_state[story_key] = {
                "stage": stage,
                "spawned_ts": time.time(),
                "stuck_escalated": False,
            }
            return True
        except Exception:
            log.exception("[%s] headless retry spawn failed", story_key)
            return False

    def _resolve_executor(self, story: dict, ctx: dict, stage: str):
        """按 story 缓存 executor（跨 tick 保留 _last_pty；换 stage 重建）。"""
        cached = self._executors.get(story["story_key"])
        if cached is not None:
            # 同 stage 复用实例；换 stage 重建（实例态 _last_pty 是 stage 维度的）
            cached_stage = getattr(cached, "_stage_for", "")
            if cached_stage == stage:
                return cached
        executor = make_stage_executor(story, ctx)
        try:
            executor._stage_for = stage
        except Exception:
            pass
        self._executors[story["story_key"]] = executor
        return executor

    def _clear_stage_state(self, story_key: str):
        self._stage_state.pop(story_key, None)
        self._executors.pop(story_key, None)

    @staticmethod
    def _next_launch_stage(actions: list, completed: list) -> str | None:
        """找第一个未完成的 launch action stage（skip action 跳过）。"""
        for a in actions or []:
            if a.get("action") == "launch":
                st = a.get("stage")
                if st and st not in completed:
                    return st
        return None

    def _complete_story(self, story_key: str, ctx: dict, actions: list):
        """所有 launch stage 完成 → story completed + 复盘/飞轮回写。"""
        from ..sourcing.state_machine import mark_completed as sm_mark_completed

        sm_mark_completed(story_key, ctx_updates=ctx)
        log.info("[%s] All stages completed", story_key)
        story = db.get_story(story_key) or {}
        workspace = story.get("workspace", "")
        try:
            from .engine.planner import _persist_playbook_for_story, _write_retrospect

            _write_retrospect(workspace, story_key, actions)
            _persist_playbook_for_story(workspace, story_key, db)
        except Exception:
            log.exception("[%s] retrospect/playbook write failed", story_key)

    # ---- judge 子线程 ----

    def _submit_judge(
        self, story_key: str, stage: str, ctx: dict, executor, story: dict
    ):
        """submit judge 到子线程池（不阻塞主循环）。

        注意（1068018 事故修复）：本函数**只**加锁去重 + 读 done_data + 存
        done_data + submit judge。**不**在此处记 completed / register / commit /
        释放 PTY —— 这些收尾动作挪到 ``_finalize_stage_pass``，仅在 judge 返回
        approve 后由 ``_handle_decision`` 调用。理由：judge 还在子线程跑时就杀
        PTY + 记 completed，reject 时 PTY 已死（没法 resume 救场）且事件流语义
        混乱（先 completed 再 rejected）。
        """
        judge_key = f"{story_key}:{stage}"
        with self._lock:
            if judge_key in self._judging:
                return
            self._judging.add(judge_key)
        workspace = story.get("workspace", "")
        actions = ctx.get("_agent_actions") or []
        done_data = _read_done_data(story_key, stage, workspace, actions)
        # done_data 存下来给 approve 后的 _finalize_stage_pass 用（register/commit
        # 要 summary/files_changed）。judge 失败兜底 approve 时可能取不到 → 收尾
        # 用空 done_data 兜底，非阻塞。
        with self._lock:
            self._stage_done_data[judge_key] = done_data
        self._executor_pool.submit(
            self._judge_task, story_key, stage, done_data, ctx, story
        )
        log.info("[%s] submit judge for stage=%s (async)", story_key, stage)

    def _finalize_stage_pass(
        self, story_key: str, stage: str, executor, story: dict, done_data: dict | None
    ):
        """judge approve 后的 stage 收尾（1068018 事故修复：从 _submit_judge 移来）。

        - 记 ``completed`` 事件 + 登记产出文件（``_register_stage_outputs``）
        - build 阶段自动 commit（``_auto_commit_worktrees``；reject 不该 commit）
        - 释放本 stage 的 PTY / headless（``_release_stage``）

        语义：**只有 approve 才算 stage 真正通过**，才记 completed / commit / 释放
        PTY。reject/escalate 不收尾 —— PTY 保留给用户介入或下轮 retry 复用。
        """
        done_data = done_data or {}
        try:
            db.log_event(story_key, stage, "completed", done_data)
            from .engine.planner import _register_stage_outputs

            _register_stage_outputs(story_key, stage, done_data)
        except Exception:
            log.exception("[%s] completed event/register outputs failed", story_key)
        if stage == "build":
            try:
                from .engine.planner import _auto_commit_worktrees

                _auto_commit_worktrees(story_key, done_data.get("summary", stage))
            except Exception:
                log.exception(
                    "[%s] auto-commit worktrees failed (non-fatal)", story_key
                )
        self._release_stage(story_key, stage, executor)
        with self._lock:
            self._stage_done_data.pop(f"{story_key}:{stage}", None)

    def _release_stage(self, story_key: str, stage: str, executor):
        """释放 stage 的 PTY/headless 进程（幂等；对齐 driver finally 收口）。"""
        try:
            pty = executor.get_pty(story_key, stage)
            if pty is None:
                return
            # sid 捕获：CLI 自分配 sid 的 adapter 在退出时捕获回填（对齐 driver finally）
            try:
                from ..infra.terminal.pty import clean_exit_pty

                clean_exit_pty(pty)
            except Exception:
                pass
            try:
                pty.kill()
            except Exception:
                pass
            try:
                from ..infra.terminal.pty import kill_pty

                kill_pty(story_key, getattr(pty, "session_id", ""))
            except Exception:
                pass
        except Exception:
            log.debug("[%s] release stage %s failed (non-fatal)", story_key, stage)

    def _judge_task(
        self, story_key: str, stage: str, done_data: dict, ctx: dict, story: dict
    ):
        """子线程：调 judge_stage_completion，结果写 _judge_results。"""
        judge_key = f"{story_key}:{stage}"
        try:
            from .evaluation.stage_completion import (
                collect_cumulative_outputs,
                judge_stage_completion,
            )

            workspace = story.get("workspace", "")
            actions = ctx.get("_agent_actions") or []
            adapter = ""
            for a in actions:
                if a.get("stage") == stage:
                    adapter = a.get("adapter", "")
                    break
            # 归一化成果物发现：与 is_artifacts_ready 用同一套（resolve_stage_artifacts），
            # 否则 judge 读空 evidence 兜底命中的文件 → 误 reject（1068018 事故）。
            from .executors import resolve_stage_artifacts

            stage_artifacts, ev, _ = resolve_stage_artifacts(story, stage)
            decision = judge_stage_completion(
                story_key=story_key,
                stage=stage,
                workspace=workspace,
                ctx=ctx,
                lifecycle_state=ctx.get("_lifecycle_state", "待启动"),
                done_data=done_data,
                cumulative_outputs=collect_cumulative_outputs(
                    workspace, story_key, actions
                ),
                adapter=adapter,
                retry_count=ctx.get("_verify_round", 1),
                story_states=_story_states(story),
                artifacts=stage_artifacts,
                evidence_candidates=ev,
            )
        except Exception:
            log.exception("[%s] judge failed for %s", story_key, stage)
            decision = {
                "quality": "approve",  # fallback
                "lifecycle_target": None,
                "summary": "",
                "reason": "judge failed, fallback approve",
            }
        with self._lock:
            self._judge_results[judge_key] = decision
            self._judging.discard(judge_key)
        log.info(
            "[%s] judge done stage=%s quality=%s",
            story_key,
            stage,
            decision.get("quality"),
        )

    def _take_judge_result(self, story_key: str, stage: str) -> dict | None:
        """主循环读 judge 结果（读后即删，单消费者）。

        只在结果真正就绪时清 _judging —— 没有结果说明子线程还在跑，
        _judging 必须保留（防重复 submit）。
        """
        judge_key = f"{story_key}:{stage}"
        with self._lock:
            result = self._judge_results.pop(judge_key, None)
            if result is not None:
                self._judging.discard(judge_key)
        return result

    # ---- 决策处理 ----

    def _handle_decision(
        self,
        story_key: str,
        stage: str,
        decision: dict,
        ctx: dict,
        actions: list,
        handler,
        executor=None,
        story: dict | None = None,
    ):
        """judge 决策三分支 → DecisionHandler。

        仅 approve 分支调 ``_finalize_stage_pass`` 收尾（记 completed / build
        commit / 释放 PTY）。reject/escalate 不收尾 —— PTY 保留给介入或 retry。
        """
        quality = decision.get("quality", "approve")
        if quality == "reject":
            handler.handle_reject(story_key, stage, decision, ctx, actions)
        elif quality == "escalate":
            handler.handle_escalate(story_key, stage, decision, ctx)
        else:
            handler.handle_approve(story_key, stage, decision, ctx, actions)
            # approve 才收尾：judge 通过了，才算 stage 真正完成（1068018 事故修复）。
            if executor is not None:
                done_data = None
                with self._lock:
                    done_data = self._stage_done_data.get(f"{story_key}:{stage}")
                self._finalize_stage_pass(
                    story_key, stage, executor, story or {}, done_data
                )
        log.info(
            "[%s] decision handled: stage=%s quality=%s",
            story_key,
            stage,
            quality,
        )


# 单例：serve 启动时 create，停时 stop。api.py lifespan 管生命周期。
_orchestrator: OrchestratorThread | None = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> OrchestratorThread:
    """获取全局编排线程实例（惰性创建）。"""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = OrchestratorThread()
            _orchestrator.start()
        return _orchestrator


def is_orchestrator_running() -> bool:
    """serve 编排线程是否在跑（区分 serve 与 CLI 场景）。"""
    global _orchestrator
    with _orchestrator_lock:
        return _orchestrator is not None and _orchestrator.is_alive()


def stop_orchestrator():
    """停编排线程（serve 停时调）。"""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is not None:
            _orchestrator.stop()
            _orchestrator = None


def drive_story_sync(
    story_key: str, max_rounds: int = 600, force_auto: bool = True
) -> str:
    """同步驱动一个 story 直到终态/暂停（CLI/swebench/测试用）。

    serve 场景编排线程自动管；CLI（story create --start / swebench solve）
    没有 serve，需要同步驱动。实现 = 循环调 OrchestratorThread._tick，
    每轮 sleep 一小段（给 judge 子线程 + PTY 反应时间），直到 story 进入
    paused/completed/failed 或达到轮次上限。

    ``force_auto``:默认 True = 旧 driver 语义(总是自动 spawn,不按 profile
    分半自动/全自动)。serve 编排线程用 profile 分(force_auto=False)。

    Returns: story 的最终 status。
    """
    from ..infra.db import models as db

    thr = OrchestratorThread(poll_interval=0, force_auto=force_auto)
    try:
        for _ in range(max_rounds):
            try:
                story = db.get_story(story_key)
                if story is None:
                    break
                thr._tick_story(story)
            except Exception:
                log.exception("[%s] drive_story_sync tick failed", story_key)
            story = db.get_story(story_key)
            if not story:
                break
            status = story.get("status", "")
            if status in ("paused", "completed", "failed"):
                return status
            time.sleep(0.2)
        story = db.get_story(story_key)
        return (story or {}).get("status", "")
    finally:
        thr.stop()
        thr._executor_pool.shutdown(wait=False)
