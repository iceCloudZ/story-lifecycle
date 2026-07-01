"""Project Profile — schema, read/write, paths for Workspace Onboarding.

Implements the Project Profile data model from the Workspace Onboarding design:
- ProjectProfile: top-level container for workspace facts
- RepoInfo: per-repo git metadata and type classification
- TestSource: discovered test command candidates
- Fact / Hypothesis: observed facts and agent hypotheses
- DocAsset / ReleaseProfile / ReleaseSignal: supporting models

Profile path: {workspace_root}/.story/project/profile.json
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PROFILE_SUBDIR = ".story/project/profile.json"


# ── Helpers ──


def workspace_id(ws: str | Path) -> str:
    """Deterministic ID from normalized workspace path."""
    normalized = str(Path(ws).resolve()).replace("\\", "/").rstrip("/")
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile_path(workspace: str | Path) -> Path:
    """Return the canonical profile.json path for a workspace."""
    return Path(workspace).resolve() / PROFILE_SUBDIR


# ── Data models ──


@dataclass
class Evidence:
    path: str = ""
    kind: str = ""


@dataclass
class RepoInfo:
    id: str = ""
    name: str = ""
    relative_path: str = ""
    git_root: str = ""
    remote: str = ""
    current_branch: str = ""
    default_branch: str = "main"
    dirty: bool = False
    repo_type: str = "unknown"  # backend|frontend|mobile|infra|docs|unknown
    confirmed: bool = False
    build_files: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class TestSource:
    id: str = ""
    repo_id: str = ""
    name: str = ""
    command: str = ""
    scope: str = "repo"  # repo|workspace
    cost: str = "medium"  # low|medium|high
    reliability: str = "unknown"  # unknown|high|low
    confirmed: bool = False
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class ReleaseSignal:
    type: str = ""
    value: Any = None


@dataclass
class ReleaseProfile:
    scale: str = "unknown"  # single_service|multi_service|frontend_backend|monorepo|multi_repo|unknown
    requires_manual_confirm: bool = True
    signals: list[ReleaseSignal] = field(default_factory=list)
    confirmed: bool = False


@dataclass
class DocAsset:
    path: str = ""
    kind: str = ""  # readme|docs_dir|ci_file


@dataclass
class Fact:
    id: str = ""
    type: str = ""
    value: str = ""
    scope: str = ""
    source: str = "deterministic_scan"
    confidence: str = "medium"  # high|medium|low
    confirmed: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    observed_at: str = ""


@dataclass
class Hypothesis:
    id: str = ""
    type: str = ""
    value: str = ""
    scope: str = ""
    source: str = "agent_probe"
    confidence: float = 0.5
    confirmed: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    observed_at: str = ""


@dataclass
class ProjectProfile:
    schema_version: int = SCHEMA_VERSION
    workspace_root: str = ""
    workspace_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    workspace_type: str = (
        "empty_or_unknown"  # single_repo|multi_repo|plain_directory|empty_or_unknown
    )
    confidence: str = "low"  # high|medium|low
    repos: list[RepoInfo] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    test_sources: list[TestSource] = field(default_factory=list)
    release_profile: ReleaseProfile = field(default_factory=ReleaseProfile)
    doc_assets: list[DocAsset] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    user_overrides: list[dict[str, Any]] = field(default_factory=list)


# ── Read / Write ──


def _to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to dicts for JSON serialization."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]
    return obj


def save_profile(workspace: str | Path, profile: ProjectProfile) -> Path:
    """Save project profile to .story/project/profile.json. Returns the path."""
    p = profile_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    if not profile.created_at:
        profile.created_at = now
    profile.updated_at = now

    p.write_text(
        json.dumps(_to_dict(profile), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def load_profile(workspace: str | Path) -> ProjectProfile | None:
    """Load project profile from .story/project/profile.json."""
    p = profile_path(workspace)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _dict_to_profile(data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def _dict_to_profile(data: dict[str, Any]) -> ProjectProfile:
    """Deserialize a dict into a ProjectProfile with nested dataclasses."""
    repos = [_dict_to_repo(r) for r in data.get("repos", [])]
    test_sources = [_dict_to_test_source(t) for t in data.get("test_sources", [])]
    release = _dict_to_release_profile(data.get("release_profile", {}))
    doc_assets = [DocAsset(**d) for d in data.get("doc_assets", [])]
    facts = [_dict_to_fact(f) for f in data.get("facts", [])]
    hypotheses = [_dict_to_hypothesis(h) for h in data.get("hypotheses", [])]

    return ProjectProfile(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        workspace_root=data.get("workspace_root", ""),
        workspace_id=data.get("workspace_id", ""),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        workspace_type=data.get("workspace_type", "empty_or_unknown"),
        confidence=data.get("confidence", "low"),
        repos=repos,
        languages=data.get("languages", []),
        test_sources=test_sources,
        release_profile=release,
        doc_assets=doc_assets,
        facts=facts,
        hypotheses=hypotheses,
        user_overrides=data.get("user_overrides", []),
    )


def _dict_to_repo(d: dict[str, Any]) -> RepoInfo:
    evidence = [Evidence(**e) for e in d.get("evidence", [])]
    return RepoInfo(
        id=d.get("id", ""),
        name=d.get("name", ""),
        relative_path=d.get("relative_path", ""),
        git_root=d.get("git_root", ""),
        remote=d.get("remote", ""),
        current_branch=d.get("current_branch", ""),
        default_branch=d.get("default_branch", "main"),
        dirty=d.get("dirty", False),
        repo_type=d.get("repo_type", "unknown"),
        confirmed=d.get("confirmed", False),
        build_files=d.get("build_files", []),
        languages=d.get("languages", []),
        evidence=evidence,
    )


def _dict_to_test_source(d: dict[str, Any]) -> TestSource:
    evidence = [Evidence(**e) for e in d.get("evidence", [])]
    return TestSource(
        id=d.get("id", ""),
        repo_id=d.get("repo_id", ""),
        name=d.get("name", ""),
        command=d.get("command", ""),
        scope=d.get("scope", "repo"),
        cost=d.get("cost", "medium"),
        reliability=d.get("reliability", "unknown"),
        confirmed=d.get("confirmed", False),
        evidence=evidence,
    )


def _dict_to_release_profile(d: dict[str, Any]) -> ReleaseProfile:
    signals = [ReleaseSignal(**s) for s in d.get("signals", [])]
    return ReleaseProfile(
        scale=d.get("scale", "unknown"),
        requires_manual_confirm=d.get("requires_manual_confirm", True),
        signals=signals,
        confirmed=d.get("confirmed", False),
    )


def _dict_to_fact(d: dict[str, Any]) -> Fact:
    evidence = [Evidence(**e) for e in d.get("evidence", [])]
    return Fact(
        id=d.get("id", ""),
        type=d.get("type", ""),
        value=d.get("value", ""),
        scope=d.get("scope", ""),
        source=d.get("source", "deterministic_scan"),
        confidence=d.get("confidence", "medium"),
        confirmed=d.get("confirmed", False),
        evidence=evidence,
        observed_at=d.get("observed_at", ""),
    )


def _dict_to_hypothesis(d: dict[str, Any]) -> Hypothesis:
    evidence = [Evidence(**e) for e in d.get("evidence", [])]
    return Hypothesis(
        id=d.get("id", ""),
        type=d.get("type", ""),
        value=d.get("value", ""),
        scope=d.get("scope", ""),
        source=d.get("source", "agent_probe"),
        confidence=d.get("confidence", 0.5),
        confirmed=d.get("confirmed", False),
        evidence=evidence,
        observed_at=d.get("observed_at", ""),
    )


# ── Refresh / Drift Detection ──


@dataclass
class DriftItem:
    type: str = ""  # repo_missing|repo_added|evidence_changed|branch_changed|dirty
    repo_id: str = ""
    severity: str = "warning"  # error|warning
    detail: str = ""


@dataclass
class RefreshReport:
    status: str = "ok"  # ok|drift|missing_profile
    drift: list[DriftItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def refresh_profile(workspace: str | Path) -> RefreshReport:
    """Story Start Refresh — lightweight drift check against existing profile."""
    ws = Path(workspace).resolve()
    profile = load_profile(ws)

    if profile is None:
        return RefreshReport(
            status="missing_profile",
            warnings=["No Project Profile found. Run `story project onboard`."],
        )

    report = RefreshReport()
    current_repo_ids: set[str] = set()

    # Check each repo in profile
    for repo in profile.repos:
        current_repo_ids.add(repo.id)
        repo_path = ws / repo.relative_path

        # Repo directory missing?
        if not repo_path.is_dir():
            report.status = "drift"
            report.drift.append(
                DriftItem(
                    type="repo_missing",
                    repo_id=repo.id,
                    severity="error",
                    detail=f"Directory not found: {repo.relative_path}",
                )
            )
            continue

        # Git repo still valid?
        if repo.git_root and not (repo_path / ".git").is_dir():
            report.status = "drift"
            report.drift.append(
                DriftItem(
                    type="repo_missing",
                    repo_id=repo.id,
                    severity="error",
                    detail=f".git directory missing in {repo.relative_path}",
                )
            )

        # Check confirmed test evidence still exists
        for ts in profile.test_sources:
            if ts.repo_id == repo.id and ts.confirmed:
                for ev in ts.evidence:
                    if not (ws / ev.path).exists():
                        report.drift.append(
                            DriftItem(
                                type="evidence_changed",
                                repo_id=repo.id,
                                severity="warning",
                                detail=f"Test evidence missing: {ev.path}",
                            )
                        )
                        if report.status == "ok":
                            report.status = "drift"

    # Detect newly added repos (shallow scan)
    if profile.workspace_type in ("multi_repo",):
        from .project_scan import _find_git_repos

        current_git = {g.name for g in _find_git_repos(ws, max_depth=4)}
        new_repos = current_git - current_repo_ids
        for name in sorted(new_repos):
            report.drift.append(
                DriftItem(
                    type="repo_added",
                    repo_id=name,
                    severity="warning",
                    detail=f"New repo detected: {name}",
                )
            )
            if report.status == "ok":
                report.status = "drift"

    return report
