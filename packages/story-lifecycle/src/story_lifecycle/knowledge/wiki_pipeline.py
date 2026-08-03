"""Wiki 条目 + draft → review → merge 管线(11-workspace-entity-design.md §4/§5)。

规则(§4.3/不变量 I2):
- ``source: human`` → 直接生效(review_state=merged),不进 draft 管线
- ``source: story:<key>`` / ``probe:<name>`` → 一律 draft,必须人工确认才 merge
- merge 时写 verified_at(人工确认时间);reject 回 draft + review_reason
- probe 产出带 evidence_refs(证据链)+ probe_snapshot(聚合快照,stale 重跑对比用)

存储:``<knowledge_root>/wiki/<slug>.md``(frontmatter + 正文),与知识层其它条目
同目录体系,INDEX.json 由 knowledge 包重新生成(I6:不建第二知识库)。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

WIKI_REVIEW_STATES = ("draft", "merged")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def wiki_dir(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / "wiki"


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "wiki-page"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _refresh_index(knowledge_root: str | Path) -> None:
    """重新生成 INDEX.json(best-effort,失败不阻断——索引是投影)。"""
    try:
        from knowledge.generator import write_index

        write_index(str(knowledge_root))
    except Exception:
        pass


def _read_entries(knowledge_root: str | Path) -> list[dict]:
    """直接扫描 wiki/ 目录读全部条目(不经 INDEX,保证最新)。"""
    d = wiki_dir(knowledge_root)
    if not d.is_dir():
        return []
    try:
        from knowledge.parser import parse_wiki
    except ImportError:
        return []
    entries: list[dict] = []
    for path in sorted(d.glob("**/*.md")):
        if path.name == "README.md" and path.parent == d:
            continue  # Phase 2 骨架占位 README 不是条目
        try:
            entry = parse_wiki(str(path), str(path.relative_to(knowledge_root)))
            entries.append(entry.to_dict())
        except Exception:
            continue
    return entries


def _write_entry_file(knowledge_root: str | Path, entry: dict) -> Path:
    import yaml

    d = wiki_dir(knowledge_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{entry['id'].split(':', 1)[-1]}.md"
    meta = {
        "id": entry["id"],
        "type": "wiki",
        "title": entry["title"],
        "summary": entry.get("summary", ""),
        "source": entry.get("source", "human"),
        "domain": entry.get("domain", ""),
        "tags": entry.get("tags", []),
        "source_refs": entry.get("source_refs", []),
        "evidence_refs": entry.get("evidence_refs", []),
        "related": entry.get("related", []),
        "review_state": entry.get("review_state", "draft"),
        "created_at": entry.get("created_at", ""),
        "updated_at": entry.get("updated_at", ""),
        "verified_at": entry.get("verified_at", ""),
        "reviewed_by": entry.get("reviewed_by", ""),
        "review_reason": entry.get("review_reason", ""),
        "probe_snapshot": entry.get("probe_snapshot", {}),
    }
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    body = (entry.get("content") or "").strip()
    path.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return path


def save_wiki_entry(
    knowledge_root: str | Path,
    *,
    title: str,
    content: str,
    source: str = "human",
    summary: str = "",
    evidence_refs: list[dict] | None = None,
    related: list[str] | None = None,
    source_refs: list[str] | None = None,
    slug: str | None = None,
    tags: list[str] | None = None,
    probe_snapshot: dict | None = None,
) -> dict:
    """创建/更新 wiki 条目。human → merged 直接生效;AI/probe → draft(I2)。

    已 merge 的页被 AI/probe 重新产出时:降级为 draft 等待人工确认(不自动覆盖)。
    返回条目 dict。
    """
    slug = slug or slugify_title(title)
    if not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid wiki slug: {slug!r} — 必须为 kebab-case")
    wiki_id = f"wiki:{slug}"
    now = _now()
    is_human = source == "human"

    existing = get_wiki_entry(knowledge_root, wiki_id)
    review_state = "merged" if is_human else "draft"
    entry = {
        "id": wiki_id,
        "type": "wiki",
        "title": title.strip(),
        "summary": (summary or "").strip(),
        "source": source,
        "domain": (existing or {}).get("domain", ""),
        "tags": tags or (existing or {}).get("tags", []),
        "source_refs": source_refs or (existing or {}).get("source_refs", []),
        "evidence_refs": evidence_refs or (existing or {}).get("evidence_refs", []),
        "related": related or (existing or {}).get("related", []),
        "review_state": review_state,
        "created_at": (existing or {}).get("created_at", ""),
        "updated_at": now,
        "verified_at": now if is_human else "",
        "reviewed_by": "human" if is_human else "",
        "review_reason": "",
        "probe_snapshot": probe_snapshot or (existing or {}).get("probe_snapshot", {}),
        "content": content.strip(),
    }
    _write_entry_file(knowledge_root, entry)
    _refresh_index(knowledge_root)
    return get_wiki_entry(knowledge_root, wiki_id) or entry


def get_wiki_entry(knowledge_root: str | Path, wiki_id: str) -> dict | None:
    for e in _read_entries(knowledge_root):
        if e["id"] == wiki_id:
            return e
    return None


def list_wiki_entries(knowledge_root: str | Path, review_state: str = "") -> list[dict]:
    """列出 wiki 条目。review_state 过滤: draft|merged|''(全部)。"""
    entries = _read_entries(knowledge_root)
    if review_state:
        entries = [e for e in entries if e.get("review_state") == review_state]
    return entries


def review_wiki(
    knowledge_root: str | Path,
    wiki_id: str,
    decision: str,
    reviewer: str = "",
    reason: str = "",
) -> dict:
    """人工确认:draft → merged(approve) 或 回 draft + reason(reject)。

    approve 写 verified_at(§4.3 人工确认时间);reject 保留 draft 态并记录原因。
    """
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision 必须是 approve|reject,收到: {decision!r}")
    entry = get_wiki_entry(knowledge_root, wiki_id)
    if not entry:
        raise KeyError(f"Wiki 条目不存在: {wiki_id}")
    now = _now()
    if decision == "approve":
        entry["review_state"] = "merged"
        entry["verified_at"] = now
        entry["reviewed_by"] = reviewer or "human"
        entry["review_reason"] = ""
    else:
        entry["review_state"] = "draft"
        entry["review_reason"] = reason or "人工打回"
    entry["updated_at"] = now
    _write_entry_file(knowledge_root, entry)
    _refresh_index(knowledge_root)
    return get_wiki_entry(knowledge_root, wiki_id) or entry


def delete_wiki(knowledge_root: str | Path, wiki_id: str) -> bool:
    for e in _read_entries(knowledge_root):
        if e["id"] == wiki_id:
            path = wiki_dir(knowledge_root) / f"{wiki_id.split(':', 1)[-1]}.md"
            path.unlink(missing_ok=True)
            _refresh_index(knowledge_root)
            return True
    return False


def generate_wiki_drafts(
    ws: dict,
    probes: list | None = None,
    config: dict | None = None,
) -> list[dict]:
    """跑 probe → 把每条证据落成 draft wiki 页(§4.3 probe 产出一律 draft)。

    - probes 缺省:config 的 wiki_probes 加载;仍为空 → 核心自带 CodeScanProbe(L1)
      (§5.4:不配 probe 时只有 L1 骨架)
    - 已 merged 的同名页跳过(不自动覆盖正式知识,I2)
    - 证据 → evidence_refs + probe_snapshot(§5.3 证据链)
    """
    from .wiki_probes.code_scan import CodeScanProbe
    from .wiki_probes import load_wiki_probes

    probes = (
        probes
        if probes is not None
        else (load_wiki_probes(config or {}) or [CodeScanProbe({})])
    )
    kroot = ws.get("knowledge_root") or _knowledge_root_from_ws(ws)
    if not kroot:
        return []
    saved: list[dict] = []
    for probe in probes:
        name = type(probe).__name__
        try:
            evidence = probe.probe(ws) or []
        except Exception:  # noqa: BLE001 — probe 失败 = 该层证据缺失,优雅降级(I4)
            continue
        for ev in evidence:
            slug = f"probe-{_probe_tag(name)}-{_slug_kind(ev.kind)}"
            existing = get_wiki_entry(kroot, f"wiki:{slug}")
            if existing and existing.get("review_state") == "merged":
                continue  # 已 merge 的正式页不自动覆盖(I2),stale 由人工决定是否重生成
            content = (
                f"## 概览\n\n{ev.summary}\n\n"
                f"## 证据\n\n- 层级: L{ev.layer}\n- 类型: {ev.kind}\n"
                + "".join(f"- {k}: {v}\n" for k, v in (ev.data or {}).items())
            )
            entry = save_wiki_entry(
                kroot,
                title=f"{ev.summary}",
                content=content,
                source=f"probe:{_probe_tag(name)}",
                summary=ev.summary,
                evidence_refs=[
                    {
                        "probe": _probe_tag(name),
                        "query": ev.query,
                        "observed_at": ev.observed_at,
                    }
                ],
                slug=slug,
                probe_snapshot=ev.data,
                source_refs=[
                    r.get("repo_path", "")
                    for r in (ws.get("repos") or [])
                    if r.get("repo_path")
                ],
            )
            saved.append(entry)
    _refresh_index(kroot)
    return saved


def _rewrite_snapshot(knowledge_root: str | Path, entry: dict) -> None:
    _write_entry_file(knowledge_root, entry)


def check_wiki_stale(
    ws: dict,
    probes: list | None = None,
    config: dict | None = None,
) -> list[dict]:
    """stale 检测(§5.3):重跑 probe 对比聚合快照 + git 语义比对(不用 mtime)。

    Returns: [{wiki_id, stale, reasons: [...]}]。
    """
    from .wiki_probes.code_scan import CodeScanProbe
    from .wiki_probes import load_wiki_probes

    probes = (
        probes
        if probes is not None
        else (load_wiki_probes(config or {}) or [CodeScanProbe({})])
    )
    kroot = ws.get("knowledge_root") or _knowledge_root_from_ws(ws)
    if not kroot:
        return []
    # 重跑 probe → (probe_tag, kind) → data 快照
    fresh: dict[tuple[str, str], dict] = {}
    for probe in probes:
        tag = _probe_tag(type(probe).__name__)
        try:
            for ev in probe.probe(ws) or []:
                fresh[(tag, _slug_kind(ev.kind))] = ev.data
        except Exception:  # noqa: BLE001
            continue

    results: list[dict] = []
    for entry in list_wiki_entries(kroot):
        reasons: list[str] = []
        # 1. probe 重跑对比
        snapshot = entry.get("probe_snapshot") or {}
        if snapshot and entry.get("evidence_refs"):
            ev_ref = entry["evidence_refs"][0]
            tag = ev_ref.get("probe", "")
            kind = entry.get("id", "").split(f"-{tag}-", 1)[-1] if tag else ""
            new_data = fresh.get((tag, kind))
            if new_data is not None and new_data != snapshot:
                reasons.append("probe 重跑对比:聚合数据变化,证据可能过期")
        # 2. git 语义比对:source_refs 文件最后变更 vs verified_at(不用 mtime)
        if entry.get("source_refs") and entry.get("verified_at"):
            try:
                from .knowledge_store.stale import (
                    _git_last_change_ts,
                    _parse_time,
                )

                root = Path(
                    kroot
                ).parent.parent  # 主工作区根(knowledge_root = <root>/.story/knowledge)
                verified = _parse_time(entry["verified_at"])
                for ref in entry["source_refs"][:5]:
                    ts = _git_last_change_ts(root, ref)
                    if ts and verified and ts > verified:
                        reasons.append(f"关联代码 {ref} 在确认后有过变更")
                        break
            except Exception:  # noqa: BLE001
                pass
        results.append(
            {"wiki_id": entry["id"], "stale": bool(reasons), "reasons": reasons}
        )
    return results


def _probe_tag(class_name: str) -> str:
    """CodeScanProbe → code-scan;EsEndOfRequestProbe → es-end-of-request。"""
    s = re.sub(r"(?<!^)(?=[A-Z])", "-", class_name).lower()
    return s.replace("_", "-").replace("probe", "").strip("-")


def _slug_kind(kind: str) -> str:
    """证据 kind → kebab:api_endpoints → api-endpoints。"""
    return re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-")


def _knowledge_root_from_ws(ws: dict) -> str | None:
    """兜底:从 ws.repos 推断知识根(无 knowledge_root 字段时)。"""
    repos = ws.get("repos") or []
    if not repos:
        return None
    first = repos[0].get("repo_path")
    if not first:
        return None
    return str(Path(first).resolve() / ".story" / "knowledge")
