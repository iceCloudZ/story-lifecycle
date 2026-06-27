"""Generate unified INDEX.json from a knowledge directory."""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone
from typing import Any

from .models import FailureEntry, KnowledgeEntry
from .parser import parse_entry, parse_failure_knowledge


def _is_markdown(path: str) -> bool:
    return path.endswith(".md")


def _attribution_reports_to_failures(knowledge_dir: str) -> list[FailureEntry]:
    """Scan failures/attribution-reports/*.json and convert to FailureEntry."""
    entries: list[FailureEntry] = []
    reports_dir = os.path.join(knowledge_dir, "failures", "attribution-reports")
    if not os.path.isdir(reports_dir):
        return entries
    for path in glob.glob(os.path.join(reports_dir, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        cat = data.get("root_cause_category") or "unknown"
        detail = data.get("root_cause_detail") or ""
        candidates = data.get("counterfactual_candidates") or []
        entries.append(
            FailureEntry(
                id=f"failure:attribution:{data.get('instance_id', os.path.basename(path))}",
                type="failure",
                title=f"{cat}: {data.get('instance_id', '')}",
                source="dynamic",
                path=os.path.relpath(path, knowledge_dir),
                category=cat,
                display_category=cat,
                detail=detail,
                stages_affected=[data.get("failure_stage", "verify")],
                mitigations=[],
                counterfactuals=candidates,
                source_refs=[os.path.relpath(path, knowledge_dir)],
            )
        )
    return entries


def _collect_entries(knowledge_dir: str) -> list[KnowledgeEntry]:
    """Scan knowledge_dir for scenarios, playbooks, and failures."""
    entries: list[KnowledgeEntry] = []
    if not os.path.isdir(knowledge_dir):
        return entries

    scenarios_dir = os.path.join(knowledge_dir, "scenarios")
    if os.path.isdir(scenarios_dir):
        for root, _, files in os.walk(scenarios_dir):
            for f in files:
                if not _is_markdown(f):
                    continue
                abs_path = os.path.join(root, f)
                rel_path = os.path.relpath(abs_path, knowledge_dir)
                entry = parse_entry(abs_path, rel_path)
                if entry:
                    entries.append(entry)

    playbooks_dir = os.path.join(knowledge_dir, "playbooks")
    if os.path.isdir(playbooks_dir):
        for root, _, files in os.walk(playbooks_dir):
            for f in files:
                if not _is_markdown(f):
                    continue
                abs_path = os.path.join(root, f)
                rel_path = os.path.relpath(abs_path, knowledge_dir)
                entry = parse_entry(abs_path, rel_path)
                if entry:
                    entries.append(entry)

    failures_path = os.path.join(knowledge_dir, "failures", "failure-knowledge.json")
    if os.path.exists(failures_path):
        entries.extend(parse_failure_knowledge(failures_path))

    # Merge attribution reports (story-lifecycle benchmarks/attribution.py)
    entries.extend(_attribution_reports_to_failures(knowledge_dir))

    return entries


def merge_attribution_reports(knowledge_dir: str) -> str:
    """Merge failures/attribution-reports/*.json into failures/failure-knowledge.json.

    Returns the path to the merged failure-knowledge.json.
    """
    failures_path = os.path.join(knowledge_dir, "failures", "failure-knowledge.json")
    payload: dict[str, Any] = {"version": 1, "failures": []}
    if os.path.exists(failures_path):
        try:
            with open(failures_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass

    existing_ids = {f.get("id") for f in payload.get("failures", [])}
    for entry in _attribution_reports_to_failures(knowledge_dir):
        if entry.id not in existing_ids:
            payload.setdefault("failures", []).append(entry.to_dict())

    os.makedirs(os.path.dirname(failures_path), exist_ok=True)
    with open(failures_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return failures_path


def _link_entries(entries: list[KnowledgeEntry]) -> None:
    """Add cross-links between scenarios, playbooks, and failures."""
    by_domain: dict[str, list[str]] = {}
    by_id = {e.id: e for e in entries}

    for e in entries:
        if e.domain:
            by_domain.setdefault(e.domain, []).append(e.id)

    for e in entries:
        if e.type == "scenario" and e.domain:
            e.links = [iid for iid in by_domain.get(e.domain, []) if iid != e.id]
        elif e.type == "playbook":
            links = []
            if e.domain and e.domain in by_domain:
                links.extend(iid for iid in by_domain[e.domain] if iid != e.id)
            for fref in getattr(e, "common_failures", []):
                fid = f"failure:{fref.category}"
                if fid in by_id and fid not in links:
                    links.append(fid)
            e.links = links
        elif e.type == "failure":
            # link back to playbooks that mention this failure
            links = []
            for pb in entries:
                if pb.type != "playbook":
                    continue
                cats = {fr.category for fr in getattr(pb, "common_failures", [])}
                if e.category in cats:
                    links.append(pb.id)
            e.links = links


def generate_index(knowledge_dir: str) -> dict[str, Any]:
    """Generate and return the unified INDEX.json payload."""
    entries = _collect_entries(knowledge_dir)
    _link_entries(entries)

    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entries": [e.to_dict() for e in entries],
    }


def write_index(knowledge_dir: str) -> str:
    """Generate and write INDEX.json to knowledge_dir."""
    payload = generate_index(knowledge_dir)
    path = os.path.join(knowledge_dir, "INDEX.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path
