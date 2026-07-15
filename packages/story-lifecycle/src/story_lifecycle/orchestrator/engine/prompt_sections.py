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

from ...knowledge import context_providers

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
    (
        "credit-limit",
        ("授信", "额度", "风控", "增信", "提额", "credit", "limit", "risk", "授信节点"),
    ),
    (
        "fund-flow",
        (
            "放款",
            "还款",
            "提现",
            "清分",
            "对账",
            "借贷",
            "repay",
            "withdraw",
            "loan",
            "fund",
        ),
    ),
    (
        "marketing",
        (
            "营销",
            "活动",
            "MGM",
            "券",
            "免息",
            "奖励",
            "coupon",
            "activity",
            "marketing",
        ),
    ),
    (
        "user-profile",
        ("用户", "资料", "认证", "隐私", "KYC", "user", "profile", "联系人"),
    ),
    ("order", ("订单", "交易", "order", "borrow", "liquidate")),
    ("integration", ("三方", "对接", "回调", "third-party", "callback", "integration")),
    (
        "gateway-infra",
        ("网关", "限流", "配置", "调度", "状态机", "gateway", "config", "infra"),
    ),
    (
        "message-notify",
        ("短信", "OTP", "通知", "模板", "whatsapp", "sms", "message", "notify", "路由"),
    ),
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


def _get_task_type(story_key: str) -> str | None:
    """查询 story 的 task_type(从 context_json,fallback 到 story_task_types.json)。

    供 build_kb_tool_section / build_design_dimensions_section 共用。
    """
    import json as _json
    task_type = None
    try:
        from ...infra.db import models as _db

        story = _db.get_story(story_key) or {}
        ctx = _json.loads(story.get("context_json") or "{}")
        task_type = ctx.get("task_type")
    except Exception:
        pass
    if not task_type:
        try:
            from pathlib import Path as _Path

            _p = (
                _Path(
                    __import__("os").environ.get(
                        "STORY_MINER_OUT",
                        "D:/github/story-lifecycle/packages/story-miner/scripts/out",
                    )
                )
                / "story_task_types.json"
            )
            if _p.exists():
                for _r in _json.loads(_p.read_text(encoding="utf-8")):
                    if _r.get("story_key") == story_key:
                        task_type = _r.get("task_type")
                        break
        except Exception:
            pass
    return task_type


def build_kb_tool_section(story_key: str, workspace: str, stage: str) -> str:
    """Build kb.py tool-guidance (agentic RAG: agent queries on-demand).

    Replaces the pre-injected knowledge_section in the FULL-AUTO executor path.
    The agent gets: its task_type + the kb.py CLI + a "must-query-before-coding"
    nudge. The agent decides when/what to query (semantic by LLM); kb.py does
    exact fetch (graph/bugs/playbook).
    """
    task_type = _get_task_type(story_key)
    if not task_type:
        return ""

    return (
        f"\n## 项目知识工具（按需查询，动手前必查）\n"
        f"本 story 归类：`{task_type}`\n\n"
        f"CLI（按需调用，别全查）：\n"
        f"```bash\n"
        f"python {_KB_PY} graph <service|table>        # 结构：调用方/表/MQ\n"
        f"python {_KB_PY} bugs {task_type}             # 风险：bug-prone 文件/磁铁\n"
        f"python {_KB_PY} bugs <file_name>             # 特定文件的 bug 历史\n"
        f"python {_KB_PY} playbook {task_type}         # 过程：高频文件/命令\n"
        f"python {_KB_PY} experience {task_type}       # 经验：历史 adapter/failure/rescue(reflect 沉淀)\n"
        f"```\n\n"
        f"**动手改代码前，先 `python {_KB_PY} bugs {task_type}` 查高风险文件 + 评估回归。**\n"
        f"**遇 verify 失败或选 adapter 时，先 `python {_KB_PY} experience {task_type}` 查历史经验。**\n"
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
        from ..evaluation.quality import build_quality_checklist

        return build_quality_checklist(story_key, stage) or ""
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""


def build_grill_protocol_section(*, interactive: bool = False) -> str:
    """通用澄清协议(grill-me):遇关键岔路可提问。

    从 design 专属推广到任意 grill=True 的 stage。
    design 的维度 checklist 仍 design-only(build_design_dimensions_section);
    本函数只负责"遇岔路可提问"的协议引导。

    Args:
        interactive: True=终端直接问人(无 MCP);False=调 mcp__lifecycle__clarify 工具。
    """
    if interactive:
        _clarify = (
            "**在终端直接问人**（一次一个：question + 2-4 个 options），拿到人答再继续"
        )
    else:
        _clarify = "**调用 `mcp__lifecycle__clarify` 工具**提问（一次一个：question + 2-4 个 options），拿到人答再继续"

    return (
        "\n## 澄清协议（grill-me）\n"
        "**遇关键岔路**（多种选择/信息缺失，不澄清就出不了正确方案）时，"
        f"{_clarify}；**基于已答内容决定下一个问，勿重复问已答过的**。\n"
        "**纪律**：只问真正卡住你的岔路（最多 3 轮）；"
        "能从代码/PRD/既有约定推断的，自己决断，不要问。\n"
    )


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


def build_design_dimensions_section(
    story_key: str,
    workspace: str,
    stage: str,
    *,
    interactive: bool = False,
) -> str:
    """design 阶段注入「设计维度 checklist + 逐问澄清 + 高价值维度 playbook」。

    与 brainstorming skill 互补:brainstorming 做发散探索,维度 checklist 做收敛/兜底,
    确保每个维度都被覆盖到(产品→技术转化)。两者共存,不互斥。

    ``interactive``:交互式终端路径(``claude "query"``,无 --mcp-config)传 True —— 逐问
    澄清改为「在终端直接问人」(交互式 claude 没 ``mcp__lifecycle__clarify`` 工具,提了会报错)。
    默认 False(自主 headless -p 路径,有 MCP)保留「调 ``mcp__lifecycle__clarify``」。

    **逐问澄清(外接 MCP)**:遇关键岔路(不澄清就出不了正确方案)时,调用
    ``mcp__lifecycle__clarify(question, options)`` 工具提问——编排层暴露的 in-process
    MCP 工具,人答经它返回,claude 带答继续(context 保留,不重 spawn)。基于前答决定下一问
    (动态澄清)。方向见 memory story-lifecycle-design-hitl(2026-07-07)。

    design-only;其他 stage 返回 ""。Failsafe:任何异常返回 checklist 骨架,不阻塞 prompt。
    高价值维度 playbook(当前 security;后续推广降级/并发/缓存)从
    ``<workspace>/.story/knowledge/playbooks/`` 窄注入(只取框架段,避撑爆 prompt)。
    """
    if stage != "design":
        return ""

    # 逐问澄清协议:交互式终端(claude "query",无 MCP)→ 在终端直接问人;
    # 自主路径(headless -p,有 MCP)→ 调 mcp__lifecycle__clarify 工具。
    if interactive:
        _clarify = (
            "**在终端直接问人**（一次一个：question + 2-4 个 options），拿到人答再继续"
        )
    else:
        _clarify = "**调用 `mcp__lifecycle__clarify` 工具**提问（一次一个：question + 2-4 个 options），拿到人答再继续"
    section = (
        "\n## 设计维度 checklist（产品→技术转化框架）\n"
        "可先用 brainstorming skill 做发散探索，再按下面维度逐个收敛做产品→技术转化，"
        "确保每个维度都覆盖到。**遇关键岔路**（多种选择/信息缺失/资方差异，且不澄清"
        f"就出不了正确方案）时，{_clarify}；**基于已答内容决定下一个问，勿重复问已答过的**。"
        "无歧义的维度直接输出一条决策点（选择 + 理由）到完成协议的 decision_points:\n"
        "1. 现状分析（现有代码/链路） 2. 架构数据流 3. 数据模型（表/字段/索引/历史数据）"
        " 4. 接口契约（API/Feign/DTO/MQ/幂等） 5. 核心逻辑（算法/状态机/事件接入点）"
        " 6. 一致性并发（对账/锁/事务） 7. 性能容量（缓存/大表/慢查询）"
        " 8. 降级兼容（灰度/兜底/回滚/新老版本） 9. 边界异常"
        " 10. 安全（鉴权/越权/输入校验/配置安全）"
        " 11. 权限 12. 风险回滚 13. 非目标\n"
        "**纪律**：只问真正卡住你的岔路（最多 3 轮）；能从代码/PRD/既有约定推断的，自己决断进 "
        "decision_points，不要问。\n"
    )

    # BUG #15: 高价值维度 playbook 改触发式引导(不全量塞框架,避免 prompt 膨胀 +
    # context rot)。遍历 dimension→playbook 映射,只注入简短引导行,claude 按需自查文件。
    # 原 MVP 全量塞 security-parameter-trust.md 的框架段(占 prompt ~60%),
    # 对后端配置类需求(灰度开关)完全不适用(满篇前端参数校验)。
    #
    # REFACTOR §5.2.2:分层路由——全局维度 playbook(根目录)+ task_type 特定经验
    # (子目录,reflect 产出)。防 skill 库相变崩溃(arxiv 2601.04748):模型只看当前
    # task_type 的子集,不面对全量。
    try:
        from pathlib import Path as _Path
        import os as _os

        _playbooks_dir = _os.environ.get(
            "STORY_PLAYBOOKS_DIR",
            str(_Path(workspace or ".") / ".story" / "knowledge" / "playbooks"),
        )
        _task_type = _get_task_type(story_key)
        # 全局 dimension → playbook 文件映射(根目录,手写 spec 蒸馏)
        _DIMENSION_PLAYBOOKS = {
            "安全": "security-parameter-trust.md",
            "降级兼容": "degradation-fallback.md",
        }
        _guides = []
        for _dim, _fname in _DIMENSION_PLAYBOOKS.items():
            _p_global = _Path(_playbooks_dir) / _fname
            if _p_global.exists():
                _guides.append(
                    f"- **{_dim}**(通用):做「{_dim}」维度时,先读 `{_p_global}`"
                    " 再做决策(框架 + 历史模式 + 常见坑)"
                )
            # task_type 特定经验(子目录,reflect 产出,阶段1)
            if _task_type:
                _p_task = _Path(_playbooks_dir) / _task_type / _fname
                if _p_task.exists():
                    _guides.append(
                        f"- **{_dim}**(本任务类型历史经验):读 `{_p_task}`"
                        "(同类任务的实战经验)"
                    )
        # reflect 产出的经验维度(adapter-routing / failure-patterns / rescue)
        if _task_type:
            _task_dir = _Path(_playbooks_dir) / _task_type
            if _task_dir.exists():
                _EXP_DIMENSIONS = {
                    "adapter-routing.md": "模型路由经验(历史 adapter 成败)",
                    "failure-patterns.md": "本任务类型高频失败模式",
                    "rescue.md": "救援经验(缺依赖怎么补)",
                }
                for _fname, _desc in _EXP_DIMENSIONS.items():
                    _p_exp = _task_dir / _fname
                    if _p_exp.exists():
                        _guides.append(f"- **{_desc}**:读 `{_p_exp}`")
        if _guides:
            section += (
                "\n### 高价值维度参考（playbook 按需自查）\n"
                + "\n".join(_guides)
                + "\n"
            )
    except Exception:  # noqa: BLE001 — never block prompt rendering
        pass

    return section


__all__ = [
    "build_knowledge_section",
    "build_quality_section",
    "build_transcript_section",
]
