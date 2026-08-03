"""Parse knowledge artifacts (markdown + JSON) into KnowledgeEntry objects."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .models import (
    CommandRef,
    FailureEntry,
    FileRef,
    FailureRef,
    KnowledgeEntry,
    PlaybookEntry,
    ScenarioEntry,
    WikiEntry,
)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _read_frontmatter(path: str) -> dict[str, Any]:
    """Best-effort read YAML frontmatter or inline JSON metadata from markdown."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    # YAML frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            import yaml
            try:
                return yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                pass
    return {}


def _slug_to_id(type_: str, path: str) -> str:
    """Generate a stable id from a knowledge file path."""
    base = os.path.splitext(os.path.basename(path))[0]
    return f"{type_}:{base}"


def _section(text: str, heading: str) -> str:
    """Extract text between a markdown heading and the next same-level heading."""
    pattern = re.compile(rf"##\s+{re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)", re.S | re.I)
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _extract_scenario_fields(path: str) -> dict[str, Any]:
    """Best-effort extraction of structured fields from legacy scenario markdown."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}

    fields: dict[str, Any] = {}

    # Participants: lines like "- hc-user: ..." or "| **hc-user** | ..."
    part = _section(text, "Participants")
    services = set()
    for line in part.splitlines():
        line = line.strip()
        if not line or line.startswith("|") and "---" in line:
            continue
        for svc in re.findall(r"\b(hc-[a-z0-9-]+)\b", line):
            services.add(svc)
    if services:
        fields["participating_services"] = sorted(services)

    # Main flow: numbered/bullet steps
    flow = _section(text, "Flow")
    steps = []
    for line in flow.splitlines():
        stripped = line.strip()
        if re.match(r"^(\d+\.|-|\*)\s+", stripped):
            steps.append(re.sub(r"^(\d+\.|-|\*)\s+", "", stripped))
    if steps:
        fields["main_flow"] = steps

    # Tables: t_xxx
    tables = _section(text, "Data Tables")
    tnames = sorted(set(re.findall(r"\b(t_[a-z0-9_]+)\b", tables, re.I)))
    if tnames:
        fields["tables"] = tnames

    # MQ topics: backtick-capped names or "Topic: ..."
    mq = _section(text, "MQ Messages")
    topics = sorted(set(re.findall(r"`([A-Z_]+)`", mq)))
    if topics:
        fields["mq_topics"] = topics

    # Source refs: file paths
    refs = _section(text, "Source Refs")
    paths = [line.strip().lstrip("- ").strip() for line in refs.splitlines() if line.strip()]
    if paths:
        fields["source_refs"] = paths

    return fields


def parse_scenario(path: str, rel_path: str) -> ScenarioEntry:
    """Parse a scenario markdown into a ScenarioEntry.

    sidecar JSON(``<scenario>.md.json``)与 frontmatter 合并读取——设计 10 改动 4:
    hc-pytest 侧 ``generate_scenarios.py`` 产出 ``{"apis": [...], "test_ref": "...",
    "updated_at": "..."}`` 的 sidecar;journey 执行状态(last_run_at/last_status)也经
    sidecar 回写。sidecar 覆盖 frontmatter(扫描结果是权威)。playbook 同款机制。
    """
    meta = _read_frontmatter(path)
    sidecar_path = path + ".json"
    sidecar = _read_json(sidecar_path) or {}
    meta.update(sidecar)
    inferred = _extract_scenario_fields(path)
    domain = os.path.basename(os.path.dirname(path)) if os.path.dirname(path) else ""
    return ScenarioEntry(
        id=meta.get("id") or _slug_to_id("scenario", path),
        type="scenario",
        title=meta.get("title") or _title_from_markdown(path),
        source=meta.get("source", "static"),
        domain=meta.get("domain", domain),
        status=meta.get("status", "extracted"),
        must_read=meta.get("must_read", inferred.get("must_read", [])),
        roles=meta.get("roles", []),
        tags=meta.get("tags", []),
        source_refs=meta.get("source_refs", inferred.get("source_refs", [])),
        path=rel_path,
        participating_services=meta.get("participating_services", inferred.get("participating_services", [])),
        main_flow=meta.get("main_flow", inferred.get("main_flow", [])),
        apis=meta.get("apis", []),
        tables=meta.get("tables", inferred.get("tables", [])),
        mq_topics=meta.get("mq_topics", inferred.get("mq_topics", [])),
        state_machines=meta.get("state_machines", []),
        known_risks=meta.get("known_risks", []),
        test_ref=meta.get("test_ref", ""),
        last_run_at=meta.get("last_run_at", ""),
        last_status=meta.get("last_status", ""),
        verified_at=meta.get("verified_at", ""),
    )


def parse_playbook(path: str, rel_path: str) -> PlaybookEntry:
    """Parse a playbook markdown + optional sidecar JSON into a PlaybookEntry."""
    meta = _read_frontmatter(path)
    sidecar_path = path + ".json"
    sidecar = _read_json(sidecar_path) or {}
    meta.update(sidecar)

    return PlaybookEntry(
        id=meta.get("id") or _slug_to_id("playbook", path),
        type="playbook",
        title=meta.get("title") or _title_from_markdown(path),
        source=meta.get("source", "dynamic"),
        domain=meta.get("domain", ""),
        status=meta.get("status", "extracted"),
        trigger=meta.get("trigger", {}),
        must_read=[f.path for f in _file_refs(meta)],
        roles=meta.get("roles", []),
        tags=meta.get("tags", []),
        source_refs=meta.get("source_refs", []),
        path=rel_path,
        theme=meta.get("theme", ""),
        session_count=meta.get("session_count", 0),
        top_files=_file_refs(meta),
        common_commands=[CommandRef.from_dict(c) for c in meta.get("common_commands", [])],
        common_failures=[FailureRef.from_dict(f) for f in meta.get("common_failures", [])],
        linked_scenarios=meta.get("linked_scenarios", []),
        linked_story=meta.get("linked_story", ""),
    )


def _file_refs(meta: dict[str, Any]) -> list[FileRef]:
    files = meta.get("top_files", [])
    if isinstance(files, list) and files and isinstance(files[0], str):
        return [FileRef(path=p) for p in files]
    return [FileRef.from_dict(f) for f in files]


def _title_from_markdown(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return os.path.splitext(os.path.basename(path))[0]


def parse_failure_knowledge(path: str) -> list[FailureEntry]:
    """Parse failures/failure-knowledge.json into FailureEntry objects."""
    data = _read_json(path)
    if not data:
        return []
    entries = data.get("failures", data) if isinstance(data, dict) else data
    out = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        out.append(
            FailureEntry(
                id=item.get("id", ""),
                type="failure",
                title=item.get("title", item.get("display_category", "")),
                source=item.get("source", "dynamic"),
                domain=item.get("domain", ""),
                status=item.get("status", "extracted"),
                tags=item.get("tags", []),
                source_refs=item.get("source_refs", []),
                path=item.get("path", ""),
                category=item.get("category", ""),
                display_category=item.get("display_category", ""),
                detail=item.get("detail", ""),
                frequency=item.get("frequency", {}),
                common_tools=item.get("common_tools", []),
                stages_affected=item.get("stages_affected", []),
                mitigations=item.get("mitigations", []),
                counterfactuals=item.get("counterfactuals", []),
            )
        )
    return out


def parse_entry(path: str, rel_path: str) -> KnowledgeEntry | None:
    """Dispatch parser based on path."""
    if "scenarios" in rel_path.split(os.sep):
        return parse_scenario(path, rel_path)
    if "playbooks" in rel_path.split(os.sep):
        return parse_playbook(path, rel_path)
    if "wiki" in rel_path.split(os.sep):
        return parse_wiki(path, rel_path)
    return None


def parse_wiki(path: str, rel_path: str) -> WikiEntry:
    """Parse a wiki markdown (frontmatter + body) into a WikiEntry.

    11-workspace-entity-design.md §4: ``type: wiki`` 条目,frontmatter 承载
    summary/review_state/evidence_refs/related/verified_at 等元数据,
    body(去 frontmatter 后的正文)是人读的完整叙述。
    """
    meta = _read_frontmatter(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    body = ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
    else:
        body = text.strip()
    domain = ""
    # domain 取 wiki 下最近子目录名(如 wiki/core-borrow/xxx.md → core-borrow)
    rel_parts = [p for p in Path(rel_path).parts if p]
    if len(rel_parts) >= 2 and rel_parts[-2] != "wiki":
        domain = rel_parts[-2]
    return WikiEntry(
        id=meta.get("id") or _slug_to_id("wiki", path),
        type="wiki",
        title=meta.get("title") or _title_from_markdown(path),
        source=meta.get("source", "human"),
        domain=meta.get("domain", domain),
        status=meta.get("status", "merged"),
        tags=meta.get("tags", []),
        source_refs=meta.get("source_refs", []),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        path=rel_path,
        links=meta.get("links", []),
        summary=meta.get("summary", ""),
        review_state=meta.get("review_state", "draft"),
        evidence_refs=meta.get("evidence_refs", []),
        related=meta.get("related", []),
        verified_at=meta.get("verified_at", ""),
        reviewed_by=meta.get("reviewed_by", ""),
        review_reason=meta.get("review_reason", ""),
        probe_snapshot=meta.get("probe_snapshot", {}),
        content=body,
    )
