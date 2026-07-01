"""Shared section builders for prompt injection.

Single source of truth for the knowledge / quality / transcript sections that
two prompt builders consume:

- ``_build_cli_prompt`` (planner.py) — full-auto live path (agent-mode).
- ``_render_prompt`` (nodes/prompt_renderer.py) — semi-auto dry-run / template
  substitution path.

Each helper returns the **raw section content** (the same text the underlying
``context_providers`` / ``quality`` functions return), or ``""`` on any failure
or when there is nothing to inject. Callers own their own wrapping (newlines,
markdown headers, stage gating) so the two paths keep their existing formatting
verbatim — this module only de-duplicates the *fetch + failsafe* logic.

These helpers intentionally never raise: prompt rendering must never be blocked
by a provider or DB error.
"""

from __future__ import annotations

from .. import context_providers

# Pure keyword classifier for task_type — mirrors the controlled vocabulary in
# ``packages/story-miner/scripts/task_type_playbooks.py::TASK_TYPE_KEYWORDS``.
# Kept here (not imported from story-miner) so story-lifecycle has zero runtime
# dependency on the miner package, and so story creation stays fast/cheap.
#
# Order matters: the first task_type whose keyword set hits wins. ``debug`` /
# ``data-sql`` / ``frontend`` / ``deploy`` are placed late because their keywords
# ("日志", "表结构", "页面", "上线"…) are common across many stories and should
# not steal a story from a more specific business domain.
TASK_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("credit-limit", ("授信", "额度", "风控", "增信", "提额", "credit", "limit", "risk", "授信节点")),
    ("fund-flow", ("放款", "还款", "提现", "清分", "对账", "借贷", "repay", "withdraw", "loan", "fund")),
    ("marketing", ("营销", "活动", "MGM", "券", "免息", "奖励", "coupon", "activity", "marketing")),
    ("user-profile", ("用户", "资料", "认证", "隐私", "KYC", "user", "profile", "联系人")),
    ("order", ("订单", "交易", "order", "borrow", "liquidate")),
    ("integration", ("三方", "对接", "回调", "third-party", "callback", "integration")),
    ("gateway-infra", ("网关", "限流", "配置", "调度", "状态机", "gateway", "config", "infra")),
    ("message-notify", ("短信", "OTP", "通知", "模板", "whatsapp", "sms", "message", "notify", "路由")),
    ("deploy", ("部署", "上线", "发版", "deploy", "release", "skyladder", "nexus")),
    ("data-sql", ("SQL", "查询", "迁移", "schema", "sql", "data", "DDL", "表结构")),
    ("frontend", ("前端", "admin", "页面", "frontend", "protable", "proform", "组件")),
    ("debug", ("排查", "定位", "debug", "为什么", "报错", "日志", "异常")),
]


def classify_task_type(title: str, description: str = "") -> str | None:
    """Classify a story title (+description) into a task_type via pure keywords.

    Returns the first matching task_type, or ``None`` if no keyword hits (the
    caller should then leave ``context_json.task_type`` unset so downstream
    providers fall back gracefully). Pure string matching — no LLM, no DB — so
    it is safe to call at story-creation time.
    """
    if not title and not description:
        return None
    haystack = f"{title or ''} {description or ''}".lower()
    for task_type, kws in TASK_TYPE_KEYWORDS:
        for kw in kws:
            if kw.lower() in haystack:
                return task_type
    return None


# Path to kb.py — the executor (claude) calls this via bash for on-demand retrieval.
_KB_PY = "D:/github/story-lifecycle/packages/story-miner/scripts/kb.py"


def build_kb_tool_section(story_key: str, workspace: str, stage: str) -> str:
    """Build kb.py tool-guidance (agentic RAG: agent queries on-demand).

    Replaces the pre-injected knowledge_section in the FULL-AUTO executor path.
    The agent gets: its task_type + the kb.py CLI + a "must-query-before-coding"
    nudge. The agent decides when/what to query (semantic by LLM); kb.py does
    exact fetch (graph/bugs/playbook).
    """
    import json as _json

    task_type = None
    try:
        from ..db import models as _db

        story = _db.get_story(story_key) or {}
        ctx = _json.loads(story.get("context_json") or "{}")
        task_type = ctx.get("task_type")
    except Exception:
        pass
    if not task_type:
        # Fallback: story_task_types.json（LLM 分类的产物）
        try:
            from pathlib import Path as _Path

            _p = _Path(
                __import__("os").environ.get(
                    "STORY_MINER_OUT",
                    "D:/github/story-lifecycle/packages/story-miner/scripts/out",
                )
            ) / "story_task_types.json"
            if _p.exists():
                for _r in _json.loads(_p.read_text(encoding="utf-8")):
                    if _r.get("story_key") == story_key:
                        task_type = _r.get("task_type")
                        break
        except Exception:
            pass
    if not task_type:
        return ""

    return (
        f"\n## 项目知识工具（按需查询，动手前必查）\n"
        f"本 story 归类：`{task_type}`\n\n"
        f"CLI（按需调用，别全查）：\n"
        f"```bash\n"
        f"python {_KB_PY} graph <service|table>     # 结构：调用方/表/MQ\n"
        f"python {_KB_PY} bugs {task_type}          # 风险：bug-prone 文件/磁铁\n"
        f"python {_KB_PY} bugs <file_name>          # 特定文件的 bug 历史\n"
        f"python {_KB_PY} playbook {task_type}      # 过程：高频文件/命令\n"
        f"```\n\n"
        f"**动手改代码前，先 `python {_KB_PY} bugs {task_type}` 查高风险文件 + 评估回归。**\n"
    )


def build_knowledge_section(story_key: str, workspace: str, stage: str) -> str:
    """Return mined knowledge context for this story/stage, or ``""``.

    Wraps ``context_providers.get_knowledge_context`` with a failsafe so prompt
    rendering is never blocked. The returned text is the provider's raw markdown
    (already includes its own ``##`` header); ``""`` means nothing to inject.
    """
    try:
        ctx = context_providers.get_knowledge_context(story_key, workspace, stage)
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""
    return ctx or ""


def build_quality_section(story_key: str, stage: str) -> str:
    """Return the compact Quality Checklist text for this story/stage, or ``""``.

    Wraps ``quality.build_quality_checklist`` with a failsafe. The returned text
    is the checklist's raw markdown (already includes its own ``## Quality
    Checklist`` header); ``""`` means nothing to inject.

    Stage gating (e.g. only inject on ``verify``) is the caller's responsibility
    so this helper serves both the template path (where the checklist slot only
    exists in ``verify.md``) and the CLI path (which gates with
    ``if stage == "verify"``) without one's semantics leaking into the other.
    """
    try:
        from .evaluation.quality import build_quality_checklist

        return build_quality_checklist(story_key, stage) or ""
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""


def build_transcript_section(story_key: str, workspace: str, stage: str) -> str:
    """Return historical transcript context for this story/stage, or ``""``.

    Wraps ``context_providers.get_transcript_context`` with a failsafe. The
    returned text is the provider's raw markdown; ``""`` means nothing to inject.
    """
    try:
        ctx = context_providers.get_transcript_context(story_key, workspace, stage)
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""
    return ctx or ""


__all__ = [
    "build_knowledge_section",
    "build_quality_section",
    "build_transcript_section",
]
