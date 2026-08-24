"""Knowledge context provider — injects mined outcome/process knowledge into prompts.

Reads phase-1/phase-2 mining artifacts from story-miner and returns a short
markdown summary for the story's task_type. Two layers are injected:

* **Outcome** (bug-prone files, cycle-time baseline, bug magnets) — the original
  section, gated on ``result_axis_phase2.json`` / ``bug_story_graph.json``.
* **Bootstrap** ("### 项目结构") — domain→service map for the story's task_type,
  sourced from ``manifest.yaml`` + ``product-context-graph.json`` so brand-new
  stories (no history yet) still get grounding context.

task_type resolution: the source of truth is the live ``story.context_json.task_type``
(set at creation by ``story_service.ensure_task_type`` — 所有创建路径的唯一打标入口).
Fallbacks, in order: the legacy ``story_task_types.json`` batch artifact for存量
stories, then a **lazy keyword backfill** (零成本分类,命中即回写 DB 自愈 —— 热路径
不放 LLM,LLM 分类已前移到创建路径)。

task_type 缺失时**不再整条放弃**(B1 ③ 降级注入):wiki 摘要、知识库检索、全局
高频失败的召回价值不依赖任务分类,此前被早退连坐砍掉。

知识根经 ``resolve_knowledge_root(workspace)`` 每次调用解析(写读同根不变量,
B4),不再是模块级硬编码常量。

The provider is intentionally lenient: missing artifacts, a missing DB, or a
parse failure result in ``None`` so prompt rendering is never blocked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..knowledge_store.paths import resolve_knowledge_root

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


def _default_miner_out() -> Path:
    """miner 产物目录,相对 monorepo 根解析(不再依赖 serve 启动 cwd)。

    ``.../packages/story-lifecycle/src/story_lifecycle/knowledge/context_providers/``
    的 parents[5] 是 ``packages/``;独立安装(无 monorepo 布局)时该目录不存在,
    provider 照常静默降级。
    """
    return Path(__file__).resolve().parents[5] / "story-miner" / "scripts" / "out"


class KnowledgeContextProvider:
    """Provide mined knowledge context for a story/stage."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # Default to story-miner output directory
        self.base = Path(
            self.config.get("base_path")
            or os.environ.get("STORY_MINER_OUT")
            or _default_miner_out()
        )

    def _knowledge_root(self, workspace: str) -> Path:
        """每次调用解析(写读同根;workspace 不同根不同)。测试 patch 此方法的
        模块级依赖 ``resolve_knowledge_root``。"""
        return resolve_knowledge_root(workspace)

    def _load(self, name: str) -> dict | list | None:
        path = self.base / name
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _story_for(self, story_key: str) -> dict | None:
        try:
            from ...infra.db import models as db

            return db.get_story(story_key)
        except Exception:
            return None

    def _task_type_from_db(self, story_key: str) -> str | None:
        """Read task_type from the live story.context_json (source of truth)."""
        story = self._story_for(story_key)
        if not story:
            return None
        try:
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

    def _lazy_keyword_backfill(self, story_key: str) -> str | None:
        """存量 story 自愈:纯关键词分类(零成本),命中即回写 DB。

        热路径不放 LLM(prompt 渲染不能多等几秒);LLM 分类已前移到创建路径
        (``story_service.ensure_task_type``),这里只兜创建时漏网的存量。
        """
        try:
            story = self._story_for(story_key) or {}
            title = story.get("title", "")
            if not title:
                return None
            from ...orchestrator.engine.prompt_sections import classify_task_type

            tt = classify_task_type(title)
            if tt:
                from ...infra.db import models as db

                db.update_context(story_key, "task_type", tt)
            return tt
        except Exception:  # noqa: BLE001 — 自愈失败不阻塞 prompt
            return None

    def _task_type_for(self, story_key: str) -> str | None:
        """Resolve task_type: live DB → legacy artifact → lazy keyword backfill."""
        tt = self._task_type_from_db(story_key)
        if tt:
            return tt
        tt = self._task_type_from_artifact(story_key)
        if tt:
            return tt
        return self._lazy_keyword_backfill(story_key)

    # ---- Bootstrap (project-structure) layer --------------------------------

    def _load_manifest_domains(self, kroot: Path) -> dict[str, tuple[str, ...]]:
        """Parse manifest.yaml spec.domains → {domain: (services,)}.

        Falls back to a static map if the file is missing/unparseable.
        """
        try:
            import yaml  # type: ignore

            path = Path(kroot) / "manifest.yaml"
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

    def _load_graph_tables(self, kroot: Path) -> dict[str, list[str]]:
        """Parse product-context-graph.json → {service: [table, ...]}.

        Returns {} on any failure; the bootstrap section degrades gracefully
        (services listed without tables).
        """
        out: dict[str, list[str]] = {}
        try:
            path = Path(kroot) / "graph" / "product-context-graph.json"
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

    def _build_bootstrap(self, task_type: str, kroot: Path) -> str:
        """Build the '### 项目结构' section for a task_type, or '' on failure."""
        domains = TASK_TYPE_DOMAINS.get(task_type)
        if not domains:
            return ""
        try:
            domain_services = self._load_manifest_domains(kroot)
            tables_by_svc = self._load_graph_tables(kroot)
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

    def _build_knowledge_index_section(
        self,
        story_key: str,
        workspace: str,
        stage: str,
        task_type: str | None,
        kroot: Path,
    ) -> str:
        """Surface playbook/scenario/failure knowledge via the ``knowledge`` contract
        package (``KnowledgeIndex.retrieve``). ISS-009 9a: turns the ④ knowledge layer
        into a runtime contract instead of aspirational. Graceful — if the package is
        not installed (lifecycle running standalone) or the knowledge dir has no
        INDEX.json, returns '' (same silent-degrade pattern as the miner soft-seam).

        ``task_type=None``(降级模式):不按 domain 过滤,改用 story 标题作 query
        关键词召回 —— 未分类 story 也能命中标题相关的 playbook/scenario。
        """
        try:
            from knowledge import KnowledgeIndex
        except ImportError:
            return ""
        try:
            idx = KnowledgeIndex(str(kroot))
        except Exception:
            return ""
        domain = (TASK_TYPE_DOMAINS.get(task_type or "") or ("",))[0]
        query = ""
        if not task_type:
            story = self._story_for(story_key) or {}
            query = story.get("title", "")
        entries = idx.retrieve(
            story_key=story_key,
            workspace=workspace,
            stage=stage,
            query=query,
            domain=domain,
            top_k=5,
        )
        if not entries:
            return ""
        out = ["### 知识库（playbook / scenario / failure）\n"]
        for e in entries:
            out.append(f"- **{e.title}** (`{e.type}`)")
            mr = getattr(e, "must_read", None) or []
            if mr:
                out.append(f"  - 必读：{', '.join(mr[:3])}")
        out.append("")
        return "\n".join(out)

    def _build_global_failures_section(self, kroot: Path) -> str:
        """全局高频失败 top 5(降级层,不依赖 task_type)。

        读 ``failures/failure-knowledge.json``,按 frequency 总和高→低取前 5。
        """
        try:
            path = Path(kroot) / "failures" / "failure-knowledge.json"
            if not path.exists():
                return ""
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f) or {}

            def _freq(item: dict) -> int:
                fr = item.get("frequency") or {}
                if isinstance(fr, dict):
                    return sum(v for v in fr.values() if isinstance(v, (int, float)))
                return int(fr) if isinstance(fr, (int, float)) else 0

            failures = data.get("failures") or []
            top = sorted(failures, key=_freq, reverse=True)[:5]
            top = [f for f in top if _freq(f) > 0]
            if not top:
                return ""
            lines = ["### 历史高频失败（全局，未按任务类型过滤）\n"]
            for f in top:
                title = (
                    f.get("display_category") or f.get("title") or f.get("category") or "?"
                )
                detail = str(f.get("detail") or "")[:60]
                line = f"- **{title}**（{_freq(f)} 次）"
                if detail:
                    line += f"：{detail}"
                lines.append(line)
                mits = [str(m) for m in (f.get("mitigations") or [])[:2]]
                if mits:
                    lines.append(f"  - 缓解：{'、'.join(mits)}")
            lines.append("")
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_wiki_summary_section(self, kroot: Path) -> str:
        """Wiki 摘要(§4.2 双读者:agent 只读 summary + related 指针)。

        - 只取 review_state=merged 的正式条目(draft 未确认不注入,I2)
        - 只注入 summary + related,不注入全文(token 稀缺,细节让 agent read_file)
        - 降权:作为知识库段之后追加的独立段(检索权低于 scenario/playbook/failure)
        - stale 标注:关联代码 git 变更晚于 verified_at → 标"可能过期,以代码为准"
        """
        try:
            from knowledge import KnowledgeIndex
        except ImportError:
            return ""
        try:
            idx = KnowledgeIndex(str(kroot))
        except Exception:
            return ""
        wiki_entries = [
            e
            for e in idx.all()
            if getattr(e, "type", "") == "wiki"
            and getattr(e, "review_state", "") == "merged"
        ]
        if not wiki_entries:
            return ""
        lines = ["### Wiki 摘要（二手知识，综述可能过期，以代码为准）\n"]
        for e in wiki_entries:
            summary = getattr(e, "summary", "") or e.title
            stale = self._wiki_is_stale(e, kroot)
            mark = "【可能过期，以代码为准】" if stale else ""
            lines.append(f"- **{e.title}**{mark}：{summary}")
            related = getattr(e, "related", None) or []
            if related:
                lines.append(f"  - 相关: {', '.join(str(r) for r in related[:5])}")
        lines.append("")
        return "\n".join(lines)

    def _wiki_is_stale(self, entry, kroot: Path) -> bool:
        """git 语义比对(§5.3,不用 mtime):source_refs 文件变更晚于 verified_at。"""
        refs = getattr(entry, "source_refs", None) or []
        verified_at = getattr(entry, "verified_at", "") or ""
        if not refs or not verified_at:
            return False
        try:
            from ..knowledge_store.stale import _git_last_change_ts, _parse_time

            root = Path(kroot).parent
            verified = _parse_time(verified_at)
            for ref in refs[:5]:
                ts = _git_last_change_ts(root, ref)
                if ts and verified and ts > verified:
                    return True
        except Exception:
            return False
        return False

    # ---- 降级层(task_type 缺失) ---------------------------------------------

    def _get_context_degraded(
        self, story_key: str, workspace: str, stage: str, kroot: Path
    ) -> str | None:
        """B1 ③:task_type 缺失时注入全局层,不再整条放弃(return None)。

        组装失败/无任何内容 → None(prompt 渲染不受影响)。
        """
        lines = ["## 飞轮知识上下文（未分类 story，降级为全局知识）\n"]
        try:
            ki = self._build_knowledge_index_section(
                story_key, workspace, stage, None, kroot
            )
            if ki:
                lines.append(ki)
        except Exception:
            pass
        try:
            gf = self._build_global_failures_section(kroot)
            if gf:
                lines.append(gf)
        except Exception:
            pass
        try:
            wiki = self._build_wiki_summary_section(kroot)
            if wiki:
                lines.append(wiki)
        except Exception:
            pass
        if len(lines) == 1:
            return None
        return "\n".join(lines)

    def get_context(self, story_key: str, workspace: str, stage: str) -> str | None:
        """Return markdown knowledge context for this story, or None."""
        task_type = self._task_type_for(story_key)
        kroot = self._knowledge_root(workspace)

        if not task_type:
            return self._get_context_degraded(story_key, workspace, stage, kroot)

        phase2 = self._load("result_axis_phase2.json") or {}
        graph = self._load("bug_story_graph.json") or {}

        lines = [f"## 飞轮知识上下文（task_type={task_type}）\n"]

        # 0. 项目结构（bootstrap layer — injected for every task_type that maps
        #    to a domain, independent of whether outcome artifacts exist.)
        try:
            bootstrap = self._build_bootstrap(task_type, kroot)
            if bootstrap:
                lines.append(bootstrap)
        except Exception:
            pass

        # 1. bug-prone 文件（仅取该类 top 5）
        patterns = (phase2.get("patterns_by_task_type") or {}).get(task_type, [])
        if patterns:
            lines.append("### 历史高风险文件\n")
            lines.append(
                "以下文件在本类任务中反复改动且关联 bug 最多，设计/实现时建议重点 review：\n"
            )
            for p in patterns[:5]:
                lines.append(
                    f"- `{p['file']}` (commits={p['commit_count']}, bug_weight={p['bug_weight']})"
                )
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
            s
            for s in stories
            if self._task_type_for(s.get("story_key", "")) == task_type
            and s.get("bug_count", 0) > 0
        ]
        type_stories.sort(key=lambda s: -s["bug_count"])
        if type_stories[:3]:
            lines.append("### 本类历史 bug 磁铁\n")
            for s in type_stories[:3]:
                lines.append(
                    f"- `{s['story_key']}` {s['title'][:50]} (bugs={s['bug_count']})"
                )
            lines.append("")

        # 4. 阶段-specific 提示
        if stage in ("design", "build"):
            lines.append("### 设计/实现建议\n")
            lines.append(
                "- 若改动涉及上述高风险文件，请在 research.md 中显式评估回归风险。\n"
            )
            lines.append(
                "- 若需求与历史 bug 磁铁业务相似，优先复用已验证的分支/模块模式。\n"
            )
        elif stage == "verify":
            lines.append("### 验证建议\n")
            lines.append(
                "- 针对上述高风险文件补充回归检查；若出现同类 bug，gate 应 block。\n"
            )

        # 5. playbook/scenario/failure 知识（④ 契约层 — via knowledge.KnowledgeIndex）
        try:
            ki_section = self._build_knowledge_index_section(
                story_key, workspace, stage, task_type, kroot
            )
            if ki_section:
                lines.append(ki_section)
        except Exception:
            pass

        # 6. wiki 摘要(§4.2:只取 summary+related,降权在知识库段之后,stale 标注)
        try:
            wiki_section = self._build_wiki_summary_section(kroot)
            if wiki_section:
                lines.append(wiki_section)
        except Exception:
            pass

        return "\n".join(lines)
