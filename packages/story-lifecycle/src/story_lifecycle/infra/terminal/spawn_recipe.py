"""Agent PTY spawn 统一配方（设计 14 §6，D3）。

两条 spawn 路径此前各自实现了一遍 resume/NEW 判定 + SessionSpec 投递 +
marker 写入 + sid 捕获（api._spawn_story_agent_pty 与
executors.InteractiveStageExecutor.spawn 的 PTY 分支，executors 自承"从
api.py 拷贝"）。本模块抽公共部分：

  resolve resume/NEW → compute_session_id → adapter.start_session →
  写 marker → ensure_agent_pty → arm sid capture → mkdir cwd

调用方（api 交互式 spawn / executors 编排线程 spawn）变成薄壳：调
``spawn_agent_pty`` + 各自特定的后处理（api 版有死后 resume 重试；
executors 版有 supervisor 接线 / _last_pty 缓存）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("story-lifecycle.spawn-recipe")

#: NEW 会话的默认 seed 提示语（api 与 executors 各自可覆盖）
DEFAULT_NEW_SEED = "请读取 `{prompt_file}` 并严格按其中的说明执行本阶段任务。"


@dataclass
class SpawnResult:
    """spawn 的完整结果 + 后处理需要的全部中间值。

    api 版的死后重试 / marker 清理需要 session_uuid / marker / use_sid /
    adapter_name，一次返回避免重复计算。
    """

    session_id: str
    pty: object
    is_resume: bool
    session_uuid: str  # 确定性 uuid5（NEW 时写 marker 的 sid）
    use_sid: str  # 实际传给 adapter 的 sid（DB 捕获值或 uuid5）
    marker: Path
    adapter_name: str
    spawn_cwd: str


def spawn_agent_pty(
    adapter,
    model: str,
    *,
    story_key: str,
    stage: str,
    workspace: str,
    spawn_cwd: str | None = None,
    seed: str | None = None,
    resume_seed: str = "继续上次的任务,完成后按完成协议写入 done 文件。",
    env: dict | None = None,
    startup_delay: float | None = None,
) -> SpawnResult:
    """统一的 agent PTY spawn 配方。api.py 和 executors.py 都走这里。

    Args:
        adapter: 已解析的 adapter（BaseAdapter 子类）。
        model: LLM model 名（传给 adapter.start_session）。
        story_key / stage / workspace: 故事三元组（resume 判据的输入）。
        spawn_cwd: agent 工作目录（workspace_path 或 workspace；None 由调用方
            决定后传入，这里不再自己猜）。
        seed: NEW 会话的完整 prompt seed（未传时用默认模板 —— 调用方通常
            传 LaunchSeedBuilder / prompt 文件路径生成的 seed）。
        resume_seed: RESUME 会话的短 seed。
        env: spawn 环境（build_story_spawn_env 产物；None 则不注入 STORY_*）。
        startup_delay: ensure_agent_pty 的启动延迟；None = 按 readiness_marker/
            pty_prompt 自动决定（有 marker 或有 prompt 需注入 → 2.0s，否则 0）。

    Returns:
        SpawnResult：session_id + pty + is_resume + 后处理中间值。
    """
    from ..db import models as db
    from ..story_paths import safe_story_path
    from .pty import ensure_agent_pty
    from .sid_capture import arm_sid_capture, now_utc_iso

    adapter_name = getattr(adapter, "name", "") or ""
    stage = stage or "design"
    if spawn_cwd:
        try:
            Path(spawn_cwd).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # 会话 NEW/RESUME 判定（与旧两条路径同源：DB session 优先 + marker 兜底）
    session_uuid = db.compute_session_id(story_key, stage, adapter_name)
    session_name = f"{story_key}-{stage}"
    marker = safe_story_path(workspace, ".story", "context", story_key) / (
        f"session_{stage}.json"
    )
    _db_row = db.get_session(story_key, stage, adapter_name)
    prespecified = bool(getattr(adapter, "prespecified_session_id", False))
    is_resume = bool(
        (_db_row and _db_row.get("session_id")) or (prespecified and marker.exists())
    )
    use_sid = (
        _db_row["session_id"] if _db_row and _db_row.get("session_id") else session_uuid
    )

    # 文件扫描捕获的时间窗口下界必须在 spawn 前取（opencode 的 session 行
    # time_created 是 CLI 启动那一刻，spawn 后取会漏掉）。
    spawn_ts = now_utc_iso()

    if seed is None:
        seed = DEFAULT_NEW_SEED.format(prompt_file=f"prompt_{stage}.md")
    spec = adapter.start_session(
        model,
        prompt=seed if not is_resume else resume_seed,
        session_id=use_sid,
        session_name=session_name,
        resume=is_resume,
    )

    if startup_delay is None:
        startup_delay = (
            0.0
            if spec.readiness_marker is None and not spec.pty_prompt
            else 2.0
        )
    session_id, pty = ensure_agent_pty(
        story_key,
        stage,
        adapter_name,
        spec.command,
        spawn_cwd,
        spec.pty_prompt,
        env=env,
        readiness_marker=spec.readiness_marker,
        startup_delay=startup_delay,
    )

    # sid 捕获（三种 sid 模型统一入口）：prespecified 无需捕获；输出行捕获
    # (kimi) 走 PTY tap 线程；文件扫描捕获 (opencode) 走 post-exit watcher。
    if adapter_name and not is_resume:
        arm_sid_capture(
            adapter,
            pty,
            story_key=story_key,
            stage=stage,
            cwd=spawn_cwd,
            since_ts=spawn_ts,
        )

    # 写 session marker（NEW 会话；resume 的会话 DB 已有 sid 无需重复捕获）
    if not is_resume:
        if adapter_name:
            try:
                db.upsert_session(
                    story_key,
                    stage,
                    adapter_name,
                    # sid 模型是 adapter 的职责（Phase 0）：prespecified 启动即知
                    # sid；否则 None 由捕获线程回填。
                    session_id=session_uuid if prespecified else None,
                )
            except Exception:
                pass
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {"session_id": session_uuid, "name": session_name, "stage": stage},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    return SpawnResult(
        session_id=session_id,
        pty=pty,
        is_resume=is_resume,
        session_uuid=session_uuid,
        use_sid=use_sid,
        marker=marker,
        adapter_name=adapter_name,
        spawn_cwd=spawn_cwd or "",
    )
