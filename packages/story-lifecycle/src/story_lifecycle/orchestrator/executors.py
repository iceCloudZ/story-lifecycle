"""StageExecutor 子类（设计 13 Step 3）— 半自动/全自动 stage 执行器。

- InteractiveStageExecutor: 半自动（等人点「启动 CLI」，编排线程只 poll+judge）
- AutomaticStageExecutor: 全自动（编排线程自动 spawn + poll + judge）

spawn 逻辑自 api.py ``_spawn_story_agent_pty``（交互式 SessionSpec 路径）与
planner.py driver 的 launch 分支（headless + PTY 全量路径）收敛而来 —— 设计 13
「一条 spawn 契约」：executor.spawn 是编排线程与 /sessions/spawn 共用的唯一入口。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .abc import StageExecutor
from ..infra import story_paths

log = logging.getLogger("story-lifecycle.executors")

# headless Popen 注册表：(story_key, stage) -> Popen。模块级全局(镜像 pty.py 的
# _ptys 注册表)—— 编排线程每轮 make_stage_executor 创建新 executor 实例,实例级
# 字典会丢注册,导致 get_pty 永远 None、每轮重复 spawn。编排线程单线程访问,无锁。
_headless_procs: dict[tuple[str, str], object] = {}
_headless_attempts: dict[tuple[str, str], int] = {}

# 进程级看门狗超时:supervise_headless_stdout 阻塞在 readline 无超时,上游 API 真挂时
# proc 会无限挂。到点强杀,让编排线程 STAGE_TIMEOUT 走 mark_failed + retry。对齐
# scheduler.STAGE_TIMEOUT=2700s;env STORY_HEADLESS_PROC_TIMEOUT 可覆盖(测试用)。
HEADLESS_PROC_TIMEOUT = float(os.environ.get("STORY_HEADLESS_PROC_TIMEOUT", "2700"))


def kill_headless(story_key: str, stage: str = "") -> None:
    """杀掉并清理 story 的 headless 进程(幂等);stage 省略则杀该 story 全部 stage。

    对齐 ``pty.kill_pty``:stage 完成 / story 删除 / serve 关停时必须连带清
    ``_headless_procs`` 注册表项 + 杀进程树,否则 headless CLI 子进程会在 story
    已删除后继续跑(真实泄漏:design 批准 + DELETE 后 opencode 仍占 ~591MB)。
    """
    keys = [
        k
        for k in list(_headless_procs)
        if k[0] == story_key and (not stage or k[1] == stage)
    ]
    for k in keys:
        proc = _headless_procs.pop(k, None)
        if proc is None:
            continue
        try:
            from .engine.planner import _kill_headless

            _kill_headless(proc)
        except Exception:
            log.warning(
                "[%s] kill_headless %s failed", story_key, k[1], exc_info=True
            )


def cleanup_headless_all() -> None:
    """serve 关停时杀掉所有残留 headless 进程(对齐 ``pty.cleanup_all``)。"""
    for k in list(_headless_procs):
        proc = _headless_procs.pop(k, None)
        if proc is None:
            continue
        try:
            from .engine.planner import _kill_headless

            _kill_headless(proc)
        except Exception:
            log.warning("cleanup_headless_all %s failed", k, exc_info=True)


def _arm_headless_watchdog(story_key: str, stage: str, proc, timeout: float | None = None) -> None:
    """起 daemon 看门狗:timeout 后 proc 仍活则强杀。

    防 supervise_headless_stdout 阻塞在 readline(无超时)—— 上游 LLM API 真挂时
    opencode proc 会无限挂、零产出(本轮 61970 是慢不是卡,但未来真挂需要兜底)。
    幂等无害:proc 已退出(poll() 非 None)则不杀。daemon 线程,关停自动清理。
    """
    import threading as _th

    if timeout is None:
        timeout = HEADLESS_PROC_TIMEOUT

    def _watch():
        _th.Event().wait(timeout)
        try:
            if proc.poll() is None:
                log.warning(
                    "[%s] headless proc %s exceeded %ss, force-killing",
                    story_key,
                    stage,
                    int(timeout),
                )
                from .engine.planner import _kill_headless

                _kill_headless(proc)
        except Exception:
            log.warning("[%s] watchdog kill failed", story_key, exc_info=True)

    _th.Thread(target=_watch, daemon=True, name=f"watchdog-h-{story_key}").start()


def load_ctx(story: dict) -> dict:
    """解析 story.context_json → dict（坏 JSON 兜底空 dict）。"""
    try:
        return json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def find_action(actions: list, stage: str) -> dict | None:
    """在 _agent_actions 里找 stage 对应的 action（launch 优先）。"""
    for a in actions or []:
        if a.get("stage") == stage:
            return a
    return None


def resolve_stage_artifacts(
    story: dict, stage: str
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """归一化的 stage 成果物发现（设计 13 bugfix）。

    返回 ``(stage_artifacts, evidence_candidates, git_worktrees)``：

    - ``stage_artifacts``: profile 里该 stage 声明的 artifacts（如
      ``["story/spec.md"]``）；profile 缺失/解析失败 → 空列表。
    - ``evidence_candidates``: ``build_evidence_candidates`` 产物
      (``{artifact: [abs_path, ...]}``)，含 evidence 子目录别名兜底。
    - ``git_worktrees``: story_project 的 worktree_path 列表（monorepo 里
      代码改动落绑定项目的 worktree，``check_artifacts_landed`` 查 ``"git"``
      artifact 时要用）。

    **这是 ``is_artifacts_ready`` 与 ``judge_stage_completion`` 共用的唯一入口**
    —— 二者必须用同一套发现结果，否则会出现「检测到落地（带 evidence 兜底）→
    judge 却读空 → 误 reject」的不一致（真实事故：1068018 design 的 spec.md 落
    evidence 目录被检测到，judge 没读到，判「成果物完全为空」reject）。
    """
    from .engine.profile_loader import resolve_profile
    from .engine.artifact_check import build_evidence_candidates

    workspace = story.get("workspace", "") or ""
    try:
        rp = resolve_profile(story.get("profile") or "minimal")
        stage_artifacts = list(rp.stage(stage).artifacts or [])
    except Exception:
        stage_artifacts = []
    if not stage_artifacts or not workspace:
        return [], {}, []
    ev = build_evidence_candidates(
        stage_artifacts, workspace, story.get("story_key", ""), story.get("title", "")
    )
    git_worktrees: list[str] = []
    try:
        from ..infra.db import models as db

        for sp in db.get_story_projects(story.get("story_key", "")):
            wt = sp.get("worktree_path") or ""
            if wt:
                git_worktrees.append(wt)
    except Exception:
        pass
    return stage_artifacts, ev, git_worktrees


@dataclass
class SpawnRequest:
    """_spawn_headless 的入参打包（设计15 阶段D：8 参数 → 1 参数对象）。

    字段 = 原 _spawn_headless 参数，类型照搬。纯数据容器，不改逻辑。
    """

    story_key: str
    stage: str
    adapter: object
    model: str
    prompt: str
    prompt_file: object
    spawn_cwd: str
    story_env: dict


class BaseStageExecutor(StageExecutor):
    """共享实现：PTY 查询 + 成果物检查 + 头文件辅助。

    ``get_pty`` 按 (story_key, stage) 查 PTY 注册表（任意 adapter）；
    ``is_artifacts_ready`` 用 profile 声明的 artifacts 判落地（含 evidence 兜底
    与 done.json 兼容视图回退）—— 与 driver poll 循环同口径。
    """

    #: 全自动 profile 名（编排线程按 profile 创建 executor）
    AUTO_PROFILES = {
        "realtest",
        "single-pass",
        "swebench",
        "headless-smoke",
        "eval-replay",
    }

    def __init__(self):
        # 本实例最近一次 spawn 的 PTY（编排线程缓存 executor 实例时可用；
        # 测试 mock ensure_agent_pty 不写真实注册表 → get_pty 兜底命中）。
        self._last_pty = None

    # ---- StageExecutor 契约 ----

    def get_pty(self, story_key: str, stage: str):
        from ..infra.terminal.pty import get_pty_for_stage

        pty = get_pty_for_stage(story_key, stage, purpose="agent")
        if pty is not None:
            return pty
        # headless 进程没有 PTY → 返回轻量代理（.alive / .kill 语义同 PTY）
        proc = _headless_procs.get((story_key, stage))
        if proc is not None:
            return _HeadlessProxy(proc)
        # 兜底：本实例最近 spawn 的 PTY（mock 不写注册表时命中；编排线程缓存
        # executor 实例才有效，直接新建实例的调用方走不到这里）。
        return self._last_pty

    def is_artifacts_ready(
        self,
        story_key: str,
        stage: str,
        *,
        pty_alive: bool = True,
        base_version: int = 0,
    ) -> bool:
        """stage 成果物是否完成（归一化判定，1068018 事故修复）。

        **只认 ``artifact_declared`` event**（agent 调 ``story tool declare`` 时写，
        version > base_version）。不看文件 —— agent 边写边存，任何"文件存在"判定
        都会撞上半成品（claude 先 Write 后 declare；spawn retry 间隙 PTY 短暂死，
        文件兜底读到 Write 的截断内容）。

        pty_alive 参数保留（调用方语义清晰）但不再分支 —— PTY 活/死都只认 declare。
        不调 declare 的 agent 会卡到 STAGE_TIMEOUT 判 failed（failed 能 /plan/regenerate
        重跑，比误判半成品 reject 安全）。
        """
        from ..infra.db import models as db

        return bool(db.get_latest_declare(story_key, stage, since_version=base_version))

    def spawn(self, story_key: str, stage: str, action: dict) -> str:
        """spawn stage 的 CLI 会话，返回 session_id。子类实现具体策略。"""
        raise NotImplementedError

    def maybe_spawn(self, story_key: str, stage: str, ctx: dict) -> None:
        """Default：半自动不自动 spawn（等人点「启动 CLI」）。"""

    # ---- 供编排线程查询 ----

    def is_headless(self, story_key: str, stage: str) -> bool:
        """当前 stage 是否 headless 模式（profile execution_mode=headless）。"""
        from ..infra.db import models as db
        from .engine.profile_loader import resolve_profile
        from .engine.execution import headless_from_profile

        story = db.get_story(story_key)
        if not story:
            return False
        try:
            return headless_from_profile(
                resolve_profile(story.get("profile") or "minimal")
            )
        except Exception:
            return False

    # ---- 子类共用：resume/NEW session spec + env ----

    def _story_env(self, story: dict, stage: str, adapter_name: str) -> dict:
        from ..infra.story_paths import build_story_spawn_env

        return build_story_spawn_env(story, stage, adapter_name)


class _HeadlessProxy:
    """headless Popen 的轻量代理：给编排线程 .alive / .kill / .session_id 语义。"""

    def __init__(self, proc):
        self._proc = proc

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def kill(self):
        from .engine.planner import _kill_headless

        _kill_headless(self._proc)

    @property
    def session_id(self) -> str:
        return getattr(self._proc, "pid", "headless")


class InteractiveStageExecutor(BaseStageExecutor):
    """半自动模式：等人点「启动 CLI」，编排线程只 poll+judge。

    ``maybe_spawn`` 不自动 spawn（继承默认 no-op）；``spawn`` 供
    /sessions/spawn 与编排线程共用（SessionSpec 契约 + arm_sid_capture）。
    """

    def spawn(self, story_key: str, stage: str, action: dict) -> str:
        """按 api._spawn_story_agent_pty 契约 spawn（交互式路径）。

        设计 14 (D3)：spawn 主体收敛到 infra/terminal/spawn_recipe.spawn_agent_pty
        （与 api._spawn_story_agent_pty 同源），本函数保留 executors 路径特有的
        _last_pty 缓存（编排线程跨 tick 查询用）。
        """
        from ..infra.db import models as db
        from ..knowledge.adapters import get_adapter
        from ..infra.terminal.spawn_recipe import spawn_agent_pty
        from .prompts import LaunchSeedBuilder

        story = db.get_story(story_key)
        if not story:
            raise ValueError(f"Story not found: {story_key}")
        workspace = story.get("workspace", "")
        stage = stage or story.get("current_stage", "design") or "design"
        ctx = load_ctx(story)

        from .engine.planner import resolve_stage_adapter

        adapter_name = resolve_stage_adapter(
            story, stage, action=action or find_action(ctx.get("_agent_actions"), stage)
        )
        adapter = get_adapter(adapter_name)
        model = getattr(adapter, "default_model", "")
        try:
            rp = resolve_profile_safe(story.get("profile") or "minimal")
            stage_cfg = rp.stage(stage) if rp else None
            if stage_cfg is not None:
                model = getattr(stage_cfg, "model", "") or model
        except Exception:
            pass

        spawn_cwd = ctx.get("workspace_path") or workspace
        seed = LaunchSeedBuilder().build(
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            ctx=ctx,
            action=action or {},
        )
        _res = spawn_agent_pty(
            adapter,
            model,
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            spawn_cwd=spawn_cwd,
            seed=seed,
            env=self._story_env(story, stage, adapter_name),
        )
        self._last_pty = _res.pty
        return _res.session_id


class AutomaticStageExecutor(BaseStageExecutor):
    """全自动模式：编排线程自动 spawn + poll + judge。

    ``maybe_spawn`` 在 stage 需要执行（launch action、未完成、无存活 PTY）时
    自动 spawn；spawn 支持 PTY 与 headless 两条路径（含 supervisor 线程）。
    """

    def maybe_spawn(self, story_key: str, stage: str, ctx: dict) -> None:
        if self._needs_spawn(story_key, stage, ctx):
            action = find_action(ctx.get("_agent_actions") or [], stage) or {
                "action": "launch",
                "stage": stage,
                "adapter": "",
                "focus": "",
            }
            self.spawn(story_key, stage, action)

    def _needs_spawn(self, story_key: str, stage: str, ctx: dict) -> bool:
        from ..infra.db import models as db

        story = db.get_story(story_key)
        if not story or story.get("status") != "active":
            return False
        if not ctx.get("_plan_confirmed"):
            return False
        if stage in list(ctx.get("_completed_stages") or []):
            return False
        action = find_action(ctx.get("_agent_actions") or [], stage)
        if not action or action.get("action") != "launch":
            return False
        pty = self.get_pty(story_key, stage)
        if pty is not None and pty.alive:
            return False
        return True

    def spawn(self, story_key: str, stage: str, action: dict) -> str:
        """全量 spawn：headless（Popen + supervise）或 PTY（SessionSpec + supervisor）。

        对齐 driver launch 分支（planner.py continue_orchestrator_agent 的 launch 段）：
        prompt 构建 → prompt 文件 → session NEW/RESUME → ensure_agent_pty →
        supervisor 线程。headless 路径写 _headless_procs 注册表（编排线程 poll）。
        """
        from ..infra.db import models as db
        from ..knowledge.adapters import get_adapter
        from .engine.planner import resolve_stage_adapter
        from .engine.profile_loader import resolve_profile

        story = db.get_story(story_key)
        if not story:
            raise ValueError(f"Story not found: {story_key}")
        workspace = story.get("workspace", "")
        ctx = load_ctx(story)

        rp = None
        profile_stages = {}
        try:
            rp = resolve_profile(story.get("profile") or "minimal")
            profile_stages = {n: c for n, c in rp.stages.items()}
        except Exception:
            pass
        headless = self.is_headless(story_key, stage)

        adapter_name = resolve_stage_adapter(story, stage, profile=rp, action=action)
        if adapter_name != (action.get("adapter") or ""):
            action["adapter"] = adapter_name
        focus = action.get("focus", "")

        # worktree 预备（build 阶段，与 driver 一致）
        if stage == "build" and db.get_story_projects(story_key):
            try:
                from ..workspace.worktree.handler import prepare_worktrees

                prepare_worktrees(story_key)
            except Exception:
                log.exception(
                    "[%s] prepare_worktrees failed; build proceeds", story_key
                )

        # prompt 构建（PromptBuilder 统一入口）
        from .prompts import get_stage_prompt_builder

        prompt = get_stage_prompt_builder(stage).build(
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            ctx=ctx,
            action=action,
        )
        from ..infra.paths import stage_done_file_rel

        done_file_rel = stage_done_file_rel(story_key, stage)
        if action.get("done_file") and action.get("done_file") != done_file_rel:
            action["done_file"] = done_file_rel

        prompt_dir = story_paths.safe_story_path(
            workspace, ".story", "context", story_key
        )
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"prompt_{stage}.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        db.update_story(story_key, current_stage=stage)

        adapter = get_adapter(adapter_name)
        model = getattr(adapter, "default_model", "")
        if stage in profile_stages:
            cfg = profile_stages[stage]
            model = (cfg.model if hasattr(cfg, "model") else "") or model

        spawn_cwd = ctx.get("workspace_path") or workspace
        try:
            Path(spawn_cwd).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # 会话 NEW/RESUME（与 driver 同源：DB session 优先）
        story_env = self._story_env(story, stage, adapter_name)

        if headless:
            return self._spawn_headless(
                SpawnRequest(
                    story_key=story_key,
                    stage=stage,
                    adapter=adapter,
                    model=model,
                    prompt=prompt,
                    prompt_file=prompt_file,
                    spawn_cwd=spawn_cwd,
                    story_env=story_env,
                )
            )

        # PTY 路径：SessionSpec 契约（与 api spawn 一致）
        return self._spawn_pty_path(
            story_key=story_key,
            stage=stage,
            adapter=adapter,
            model=model,
            adapter_name=adapter_name,
            prompt_file=prompt_file,
            prompt=prompt,
            spawn_cwd=spawn_cwd,
            story_env=story_env,
            story=story,
            workspace=workspace,
            focus=focus,
            ctx=ctx,
        )

    def _spawn_pty_path(
        self,
        *,
        story_key,
        stage,
        adapter,
        model,
        adapter_name,
        prompt_file,
        prompt,
        spawn_cwd,
        story_env,
        story,
        workspace,
        focus,
        ctx,
    ):
        """PTY spawn 路径（设计15 阶段D 从 spawn 抽出）：SessionSpec 契约 + supervisor。

        NEW/RESUME 判定 → adapter.start_session → ensure_agent_pty → supervisor 线程。
        与 api spawn 路径同一套 SessionSpec 契约。
        """
        from ..infra.db import models as db
        from ..infra.terminal.pty import ensure_agent_pty
        from ..infra.terminal.pty_logger import PtyLogger
        from ..infra.db import models as _sd

        _prior = _sd.get_session(story_key, stage, adapter_name)
        if _prior and _prior.get("session_id"):
            resume_seed = "继续上次的任务,完成后用 `story tool declare` 落地成果物。"
            spec = adapter.start_session(
                model=model,
                prompt=resume_seed,
                session_id=_prior["session_id"],
                resume=True,
            )
            log.info(
                "[%s] RESUME session stage=%s adapter=%s sid=%s",
                story_key,
                stage,
                adapter_name,
                _prior["session_id"],
            )
        else:
            new_sid = _sd.compute_session_id(story_key, stage, adapter_name)
            seed = (
                f"请读取 `{prompt_file}` 并严格按其中的说明执行本阶段"
                f"({stage})任务,完成后用 `story tool declare` 落地成果物。"
            )
            spec = adapter.start_session(
                model=model, prompt=seed, session_id=new_sid, resume=False
            )
            try:
                _sd.upsert_session(
                    story_key,
                    stage,
                    adapter_name,
                    session_id=new_sid if adapter.prespecified_session_id else None,
                )
            except Exception:
                pass
            log.info(
                "[%s] NEW session stage=%s adapter=%s", story_key, stage, adapter_name
            )
        launch_cmd = spec.command

        # anchor（I2 miner binding，best-effort）
        try:
            adapter.write_anchor(
                prompt=spec.pty_prompt if spec else prompt,
                story_key=story_key,
                stage=stage,
                cwd=spawn_cwd,
                workspace=workspace,
            )
        except Exception:
            pass

        pty_logger = None
        try:
            pty_logger = PtyLogger(story_key, stage, spawn_cwd)
            try:
                db.update_session_trace(
                    story_key, stage, adapter_name, pty_log_ref=pty_logger.log_ref
                )
            except Exception:
                pass
        except Exception:
            log.debug("PtyLogger init failed (non-fatal)", exc_info=True)

        _, pty = ensure_agent_pty(
            story_key,
            stage,
            adapter_name,
            launch_cmd,
            spawn_cwd,
            spec.pty_prompt if spec else "",
            readiness_marker=spec.readiness_marker if spec else None,
            env=story_env,
            logger=pty_logger,
        )
        log.info("[%s] PTY session started for stage=%s", story_key, stage)

        # supervisor 线程（awaiting 检测 + auto-confirm；对齐 driver）
        self._start_supervisor(story_key, stage, adapter_name, pty, focus, ctx)
        self._last_pty = pty

        return getattr(pty, "session_id", "")

    def _spawn_headless(self, req: "SpawnRequest") -> str:
        """headless 路径：Popen + supervise_headless_stdout drain 线程。"""
        story_key = req.story_key
        stage = req.stage
        adapter = req.adapter
        model = req.model
        prompt = req.prompt
        prompt_file = req.prompt_file  # noqa: F841 — 保持与 SpawnRequest 字段对应
        spawn_cwd = req.spawn_cwd
        story_env = req.story_env
        import subprocess as _sp

        from ..infra.llm_client import get_llm
        from ..infra.db import models as db

        launch_cmd = adapter.headless_launch_cmd(model=model, prompt="")
        try:
            adapter.write_anchor(
                prompt=prompt,
                story_key=story_key,
                stage=stage,
                cwd=spawn_cwd,
                workspace=spawn_cwd,
            )
        except Exception:
            pass
        log.info("[%s] HEADLESS spawn stage=%s cmd=%s", story_key, stage, launch_cmd)
        try:
            proc = _sp.Popen(
                launch_cmd,
                cwd=spawn_cwd,
                stdin=_sp.PIPE,
                stdout=_sp.PIPE,
                stderr=_sp.PIPE,
                env=story_env,
                # 隔离进程组:opencode 进自己的 session/pgid,这样 kill_tree 的
                # killpg(getpgid(child)) 只杀 opencode+子,不连杀 serve 的进程组
                # (2026-08-12 真实事故:delete story → kill_headless → kill_tree →
                # killpg 打到 serve 的组 → serve 被杀;Restart 兜底但每删一 story 就重起)。
                start_new_session=True,
            )
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except Exception as exc:
            from ..sourcing.state_machine import mark_failed as sm_mark_failed

            sm_mark_failed(story_key, f"Stage {stage} headless spawn failed: {exc}")
            raise

        key = (story_key, stage)
        _headless_procs[key] = proc
        _headless_attempts[key] = _headless_attempts.get(key, 0) + 1

        # drain + supervisor（对齐 driver）
        try:
            import threading as _th
            from .engine.claude_stream import supervise_headless_stdout

            def _drain():
                try:
                    supervise_headless_stdout(
                        proc=proc,
                        adapter=self._adapter_name_for(story_key, stage),
                        story_facts={"story_key": story_key, "stage": stage},
                        llm_invoke=get_llm().invoke,
                        log_event_fn=db.log_event,
                        stderr_tail=[],
                    )
                except Exception:
                    log.exception(
                        "[%s] headless stdout drain thread died", story_key
                    )

            _th.Thread(
                target=_drain, daemon=True, name=f"supervise-h-{story_key}"
            ).start()
        except Exception:
            log.warning(
                "[%s] headless drain thread spawn failed", story_key, exc_info=True
            )
        # 进程级看门狗:超时强杀(防 supervise readline 无超时,上游 API 真挂)
        try:
            _arm_headless_watchdog(story_key, stage, proc)
        except Exception:
            log.warning("[%s] arm watchdog failed", story_key, exc_info=True)
        return str(getattr(proc, "pid", "headless"))

    def _adapter_name_for(self, story_key: str, stage: str) -> str:
        from ..infra.db import models as db

        story = db.get_story(story_key)
        if not story:
            return ""
        ctx = load_ctx(story)
        action = find_action(ctx.get("_agent_actions") or [], stage)
        from .engine.planner import resolve_stage_adapter

        return resolve_stage_adapter(story, stage, action=action)

    def _start_supervisor(
        self, story_key, stage, adapter_name, pty, focus, ctx
    ) -> None:
        """PTY supervisor 线程（awaiting 检测 + auto-confirm；对齐 driver）。"""
        try:
            import asyncio as _aio
            import threading as _th

            from ..infra.db import models as db
            from ..infra.llm_client import get_llm
            from .engine.awaiting_detector import make_awaiting_fn
            from .engine.execution import auto_confirm_from_profile
            from .engine.profile_loader import resolve_profile
            from .engine.supervisor import supervise_pty_session

            rp = None
            try:
                rp = resolve_profile(
                    (db.get_story(story_key) or {}).get("profile") or "minimal"
                )
            except Exception:
                pass
            story_facts = {
                "story_key": story_key,
                "stage": stage,
                "summary": focus,
                "auto_confirm": auto_confirm_from_profile(rp, stage),
            }
            sup_pty = pty
            sup_det = make_awaiting_fn(adapter_name)

            def _supervise():
                try:
                    loop = _aio.new_event_loop()
                    _aio.set_event_loop(loop)
                    loop.run_until_complete(
                        supervise_pty_session(
                            pty=sup_pty,
                            adapter=adapter_name,
                            story_facts=story_facts,
                            is_awaiting_fn=sup_det,
                            llm_invoke=get_llm().invoke,
                            log_event_fn=db.log_event,
                        )
                    )
                except Exception:
                    log.exception("[%s] PTY supervisor thread died", story_key)

            _th.Thread(
                target=_supervise, daemon=True, name=f"supervise-p-{story_key}"
            ).start()
        except Exception:
            log.warning(
                "[%s] PTY supervisor thread spawn failed", story_key, exc_info=True
            )


def resolve_profile_safe(profile_name: str):
    from .engine.profile_loader import resolve_profile

    try:
        return resolve_profile(profile_name)
    except Exception:
        return None


def make_stage_executor(story: dict, ctx: dict) -> StageExecutor:
    """Factory：按 profile + 上下文创建 StageExecutor。

    全自动 profile（realtest/single-pass/swebench/headless-smoke/eval-replay）→
    AutomaticStageExecutor；其余（minimal/strict/demo）→ InteractiveStageExecutor。
    """
    profile_name = (story or {}).get("profile") or "minimal"
    if profile_name in AutomaticStageExecutor.AUTO_PROFILES:
        return AutomaticStageExecutor()
    # single-pass 语义（单 stage 包干）也按全自动走
    try:
        rp = resolve_profile_safe(profile_name)
        if rp is not None and len(getattr(rp, "stages", {}) or {}) <= 1:
            return AutomaticStageExecutor()
    except Exception:
        pass
    return InteractiveStageExecutor()
