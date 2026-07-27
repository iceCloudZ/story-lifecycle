"""无状态上下文组装(STEP 2.4,DESIGN §4.6 / §7.7)。

编排 LLM 每次唤起是无状态短调用(不 resume 长会话)。本模块从 DB 组装喂判定 LLM
的上下文:PRD + 当前成果物内容 + 执行轨迹(story_session)+ 决策历史(orchestrator_decision)。

**为什么无状态**(§4.6):避压缩失忆 + 决策全落库是审计载体。每次唤起从 db 拼完整前情。

**裁剪策略**(§7.7):长 story 上下文膨胀。本模块:
- 决策历史只取最近 N 条(默认 5)
- 成果物内容每文件截断(默认前 2000 字符)
- PRD 全量(它是判定基准,不裁剪)
- 执行轨迹只取当前 stage 的 session(不卷入其他 stage)

纯读 DB + 文件,零 LLM,零副作用。boundary_judge / stuck_diagnose 都调它。
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("story-lifecycle.judge_context")

# 裁剪阈值(§7.7)。
MAX_DECISIONS = 5  # 决策历史最多喂最近 5 条(老的摘要化或丢弃)
MAX_ARTIFACT_CHARS = 2000  # 单个成果物文件内容截断前 2000 字符
MAX_EVENTS_FOR_TRACE = 20  # 执行轨迹(events.jsonl)喂最近 20 条


def assemble_judge_context(
    story_key: str,
    stage: str,
    workspace: str,
    *,
    artifacts: list[str] | None = None,
    adapter: str = "",
    db_module=None,
) -> dict:
    """组装喂判定 LLM 的上下文(无状态,§4.6)。

    Args:
        story_key / stage: 当前判定的 story+stage。
        workspace: 工作区根(读成果物文件 + events.jsonl)。
        artifacts: 该 stage 的成果物路径(相对 workspace)。None 时尝试从 profile 读。
        adapter: 当前 stage 的 adapter(查 session 执行轨迹用)。空则查该 stage 所有 session。
        db_module: 注入 db(测试用);None 则延迟 import。

    Returns:
        {
            "story_key", "stage",
            "prd": str,                      # PRD 全量(基准,不裁剪)
            "artifacts": [{path, content}],  # 成果物内容(每文件截断 MAX_ARTIFACT_CHARS)
            "execution_trace": {...},        # 当前 stage 的 session 执行轨迹
            "decision_history": [...],       # 最近 MAX_DECISIONS 条编排决策
        }
        缺字段(无 PRD / 无 session / 无决策)→ 空值,不崩。
    """
    if db_module is None:
        from ...infra.db import models as db_module

    ctx = {
        "story_key": story_key,
        "stage": stage,
        "prd": "",
        "artifacts": [],
        "execution_trace": {},
        "decision_history": [],
    }

    # 1. PRD(全量,基准)
    try:
        prd_doc = db_module.get_story_doc(story_key, "prd")
        if prd_doc and prd_doc.get("latest_content"):
            ctx["prd"] = prd_doc["latest_content"]
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] read prd failed (non-fatal): %s", story_key, exc)

    # 2. 成果物内容(每文件截断)
    if artifacts:
        for art in artifacts:
            if not isinstance(art, str) or art in ("git",) or not art:
                continue
            content = _read_artifact_content(art, workspace)
            if content is not None:
                ctx["artifacts"].append(
                    {"path": art, "content": content[:MAX_ARTIFACT_CHARS]}
                )

    # 3. 执行轨迹(当前 stage 的 session)
    ctx["execution_trace"] = _assemble_trace(
        story_key, stage, adapter, workspace, db_module
    )

    # 4. 决策历史(最近 MAX_DECISIONS 条,§7.7 裁剪)
    try:
        decisions = db_module.get_decisions(story_key, stage, limit=MAX_DECISIONS)
        ctx["decision_history"] = [
            {
                "trigger": d.get("trigger"),
                "decision": d.get("decision"),
                "reason": d.get("reason", ""),
                "decided_at": d.get("decided_at"),
            }
            for d in decisions
        ]
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] read decisions failed (non-fatal): %s", story_key, exc)

    return ctx


def _read_artifact_content(artifact: str, workspace: str) -> str | None:
    """读成果物文件内容(相对 workspace)。不存在/读失败 → None。"""
    try:
        p = Path(workspace) / artifact
        if not p.exists() or not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _assemble_trace(
    story_key: str, stage: str, adapter: str, workspace: str, db_module
) -> dict:
    """组装当前 stage 的执行轨迹(story_session 字段 + events.jsonl 摘要)。"""
    trace: dict = {"session": None, "recent_events": []}

    # session 执行轨迹(STEP 1 加的 attempt/outcome/failure_reason/artifacts_prod/pty_log_ref)
    try:
        if adapter:
            session = db_module.get_session(story_key, stage, adapter)
        else:
            # 无 adapter → 查该 stage 任意一条 session
            sessions = db_module.list_sessions_for_story(story_key)
            session = next((s for s in sessions if s.get("stage") == stage), None)
        if session:
            trace["session"] = {
                "adapter": session.get("adapter"),
                "attempt": session.get("attempt"),
                "outcome": session.get("outcome"),
                "failure_reason": session.get("failure_reason"),
                "artifacts_prod": session.get("artifacts_prod"),
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] read session trace failed (non-fatal): %s", story_key, exc)

    # events.jsonl 摘要(最近 N 条,喂卡住诊断用;边界判定一般用不上但组进上下文供审计)
    try:
        # events.jsonl 在 worktree,但 evidence_dir 是 story-doc 位置。events 实际在
        # <spawn_cwd>/.story/runs/<key>/pty_<stage>/events.jsonl。这里 best-effort 读
        # workspace 下的(可能不准,但 boundary_judge 主要用 PRD+成果物+决策,events 是
        # stuck_diagnose 自己读的)。
        events_path = (
            Path(workspace)
            / ".story"
            / "runs"
            / story_key
            / f"pty_{stage}"
            / "events.jsonl"
        )
        if events_path.exists():
            from ...infra.terminal.pty_logger import read_events

            events = read_events(events_path, limit=MAX_EVENTS_FOR_TRACE)
            trace["recent_events"] = [
                {
                    "ts": e.get("ts"),
                    "dir": e.get("dir"),
                    "text": (e.get("text") or "")[:200],
                }
                for e in events
            ]
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] read events trace failed (non-fatal): %s", story_key, exc)

    return trace


def context_ref(ctx: dict) -> str:
    """生成上下文引用标识(短 hash,写 orchestrator_decision.context_ref 审计用)。

    让审计能追溯"这次决策喂了什么上下文"——不存全文(太大),存可定位的引用。
    """
    import hashlib

    h = hashlib.sha256()
    h.update(ctx.get("prd", "").encode("utf-8")[:500])  # PRD 头部
    for a in ctx.get("artifacts", []):
        h.update(a.get("path", "").encode("utf-8"))
    for d in ctx.get("decision_history", []):
        h.update((d.get("decision", "") + d.get("reason", "")).encode("utf-8"))
    return f"ctx:{h.hexdigest()[:12]}"
