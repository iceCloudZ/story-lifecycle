"""Knowledge context provider — injects mined outcome/process knowledge into prompts.

Reads phase-1/phase-2 mining artifacts from story-miner and returns a short
markdown summary for the story's task_type. Two layers are injected:

* **Outcome** (bug-prone files, cycle-time baseline, bug magnets) — the original
  section, gated on ``result_axis_phase2.json`` / ``bug_story_graph.json``.
* **Bootstrap** ("### 项目结构") — domain→service map for the story's task_type,
  sourced from ``manifest.yaml`` + ``product-context-graph.json`` so brand-new
  stories (no history yet) still get grounding context.

task_type resolution: the source of truth is the live ``story.context_json.task_type``
(set at creation by the keyword classifier in ``orchestrator.prompt_sections``).
We fall back to the legacy ``story_task_types.json`` batch artifact for存量 stories
that predate auto-tagging.

The provider is intentionally lenient: missing artifacts, a missing DB, or a
parse failure result in ``None`` so prompt rendering is never blocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# task_type → list of business domains to surface in the bootstrap section.
# Domains are keys in manifest.yaml spec.domains[].name and graph Domain nodes.
# Tuned against the actual manifest (core-business/risk-management/channel-payment/
# infrastructure/operations) so each task_type maps to real services.
TASK_TYPE_DOMAINS: dict[str, tuple[str, ...]] = {
    "credit-limit": ("core-business", "risk-management"),
    "fund-flow": ("core-business",),
    "marketing": ("operations",),
    "user-profile": ("core-business",),
    "order": ("core-business",),
    "integration": ("channel-payment",),
    "gateway-infra": ("infrastructure",),
    "message-notify": ("operations",),
}

# Defaults if manifest.yaml is missing — mirrors the manifest's domain→service map.
_DEFAULT_DOMAIN_SERVICES: dict[str, tuple[str, ...]] = {
    "core-business": ("hc-user", "hc-order", "hc-limit"),
    "risk-management": ("hc-risk-management", "hc-audit"),
    "channel-payment": ("hc-third-party", "hc-callback"),
    "infrastructure": ("hc-gateway", "hc-config", "hc-job"),
    "operations": ("hc-coupon", "hc-marketing", "hc-message"),
}

# Where the hc-all knowledge package lives. Override via env for tests/other repos.
_KNOWLEDGE_ROOT = Path(
    os.environ.get("STORY_KNOWLEDGE_ROOT", "D:/hc-all/.story/knowledge")
)


class KnowledgeContextProvider:
    """Provide mined knowledge context for a story/stage."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # Default to story-miner output directory
        self.base = Path(
            self.config.get("base_path")
            or os.environ.get("STORY_MINER_OUT")
            or "packages/story-miner/scripts/out"
        )

    def _load(self, name: str) -> dict | list | None:
        path = self.base / name
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _task_type_from_db(self, story_key: str) -> str | None:
        """Read task_type from the live story.context_json (source of truth)."""
        try:
            from ..db import models as db

            story = db.get_story(story_key)
            if not story:
                return None
            ctx_str = story.get("context_json") or "{}"
            ctx = json.loads(ctx_str) if isinstance(ctx_str, str) else (ctx_str or {})
            tt = ctx.get("task_type")
            return tt or None
        except Exception:
            return None

    def _task_type_from_artifact(self, story_key: str) -> str | None:
        """Legacy fallback: read from the one-shot story_task_types.json batch."""
        data = self._load("story_task_types.json")
        if not isinstance(data, list):
            return None
        for r in data:
            if r.get("story_key") == story_key:
                return r.get("task_type")
        return None

    def _task_type_for(self, story_key: str) -> str | None:
        """Resolve task_type: live DB context first, then legacy artifact."""
        tt = self._task_type_from_db(story_key)
        if tt:
            return tt
        return self._task_type_from_artifact(story_key)

    # ---- Bootstrap (project-structure) layer --------------------------------

    def _load_manifest_domains(self) -> dict[str, tuple[str, ...]]:
        """Parse manifest.yaml spec.domains → {domain: (services,)}.

        Falls back to a static map if the file is missing/unparseable.
        """
        try:
            import yaml  # type: ignore

            path = _KNOWLEDGE_ROOT / "manifest.yaml"
            if not path.exists():
                return dict(_DEFAULT_DOMAIN_SERVICES)
            with path.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            domains = {}
            for d in (doc.get("spec") or {}).get("domains") or []:
                name = d.get("name")
                svcs = tuple(d.get("services") or [])
                if name and svcs:
                    domains[name] = svcs
            return domains or dict(_DEFAULT_DOMAIN_SERVICES)
        except Exception:
            return dict(_DEFAULT_DOMAIN_SERVICES)

    def _load_graph_tables(self) -> dict[str, list[str]]:
        """Parse product-context-graph.json → {service: [table, ...]}.

        Returns {} on any failure; the bootstrap section degrades gracefully
        (services listed without tables).
        """
        out: dict[str, list[str]] = {}
        try:
            path = _KNOWLEDGE_ROOT / "graph" / "product-context-graph.json"
            if not path.exists():
                return out
            with path.open("r", encoding="utf-8") as f:
                doc = json.load(f) or {}
            for n in doc.get("nodes") or []:
                if n.get("type") == "Table":
                    svc = n.get("service")
                    name = n.get("name")
                    if svc and name:
                        out.setdefault(svc, []).append(name)
        except Exception:
            return out
        return out

    def _build_bootstrap(self, task_type: str) -> str:
        """Build the '### 项目结构' section for a task_type, or '' on failure."""
        domains = TASK_TYPE_DOMAINS.get(task_type)
        if not domains:
            return ""
        try:
            domain_services = self._load_manifest_domains()
            tables_by_svc = self._load_graph_tables()
        except Exception:
            return ""
        if not domain_services:
            return ""

        lines = ["### 项目结构\n"]
        lines.append(
            f"本类任务（`{task_type}`）主要涉及以下业务域与服务，"
            "改动应聚焦于此，跨域变更需显式确认依赖：\n"
        )
        for d in domains:
            svcs = domain_services.get(d)
            if not svcs:
                continue
            lines.append(f"- **{d}**：" + "、".join(f"`{s}`" for s in svcs))
            for s in svcs:
                tbls = tables_by_svc.get(s)
                if tbls:
                    shown = "、".join(f"`{t}`" for t in tbls[:4])
                    lines.append(f"  - {s} 关键表：{shown}")
        lines.append("")
        return "\n".join(lines)

    def get_context(self, story_key: str, workspace: str, stage: str) -> str | None:
        """Return markdown knowledge context for this story, or None."""
        task_type = self._task_type_for(story_key)
        if not task_type:
            return None

        phase2 = self._load("result_axis_phase2.json") or {}
        graph = self._load("bug_story_graph.json") or {}

        lines = [f"## 飞轮知识上下文（task_type={task_type}）\n"]

        # 0. 项目结构（bootstrap layer — injected for every task_type that maps
        #    to a domain, independent of whether outcome artifacts exist.)
        try:
            bootstrap = self._build_bootstrap(task_type)
            if bootstrap:
                lines.append(bootstrap)
        except Exception:
            pass

        # 1. bug-prone 文件（仅取该类 top 5）
        patterns = (phase2.get("patterns_by_task_type") or {}).get(task_type, [])
        if patterns:
            lines.append("### 历史高风险文件\n")
            lines.append("以下文件在本类任务中反复改动且关联 bug 最多，设计/实现时建议重点 review：\n")
            for p in patterns[:5]:
                lines.append(f"- `{p['file']}` (commits={p['commit_count']}, bug_weight={p['bug_weight']})")
            lines.append("")

        # 2. cycle-time 基线
        ct = (phase2.get("cycle_time") or {}).get("by_task_type", {}).get(task_type)
        if ct and ct.get("n"):
            lines.append("### 同类 bug 修复耗时基线\n")
            lines.append(
                f"- median={ct['median']}h, p90={ct['p90']}h, mean={ct['mean']}h (n={ct['n']})\n"
            )

        # 3. 该类 top bug 磁铁（警示）
        stories = graph.get("stories", [])
        type_stories = [
            s for s in stories
            if self._task_type_for(s.get("story_key", "")) == task_type and s.get("bug_count", 0) > 0
        ]
        type_stories.sort(key=lambda s: -s["bug_count"])
        if type_stories[:3]:
            lines.append("### 本类历史 bug 磁铁\n")
            for s in type_stories[:3]:
                lines.append(f"- `{s['story_key']}` {s['title'][:50]} (bugs={s['bug_count']})")
            lines.append("")

        # 4. 阶段-specific 提示
        if stage in ("design", "build"):
            lines.append("### 设计/实现建议\n")
            lines.append("- 若改动涉及上述高风险文件，请在 research.md 中显式评估回归风险。\n")
            lines.append("- 若需求与历史 bug 磁铁业务相似，优先复用已验证的分支/模块模式。\n")
        elif stage == "verify":
            lines.append("### 验证建议\n")
            lines.append("- 针对上述高风险文件补充回归检查；若出现同类 bug，gate 应 block。\n")

        return "\n".join(lines)
