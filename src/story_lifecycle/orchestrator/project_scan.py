"""Deterministic workspace scanner — produces observed facts without AI.

Implements the deterministic scan portion of the Workspace Onboarding design:
- Workspace type classification
- Repo inventory with git info
- Test command discovery
- Release/scale signal detection
- Doc asset discovery
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .project_profile import (
    DocAsset,
    Evidence,
    Fact,
    ProjectProfile,
    ReleaseProfile,
    ReleaseSignal,
    RepoInfo,
    TestSource,
    workspace_id,
)

# Directories to skip during scan
_IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "target",
        ".gradle",
        ".mvn",
        "build",
        "dist",
        ".umi",
        "__pycache__",
        ".git",
        ".svn",
        ".idea",
        ".vscode",
        ".codegraph",
        "venv",
        ".venv",
        "env",
        ".story",
        ".next",
        ".nuxt",
        "out",
        "bin",
        "obj",
    }
)

# Build files → language
_BUILD_FILE_LANG: dict[str, str] = {
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "package.json": "javascript",
    "pyproject.toml": "python",
    "setup.py": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "Gemfile": "ruby",
    "composer.json": "php",
}

# Build file → test command candidates
_TEST_COMMAND_MAP: dict[str, list[tuple[str, str]]] = {
    "pom.xml": [("mvn test", "maven")],
    "build.gradle": [("./gradlew test", "gradle")],
    "build.gradle.kts": [("./gradlew test", "gradle")],
    "package.json": [("npm test", "npm")],
    "pyproject.toml": [("pytest", "pytest")],
    "go.mod": [("go test ./...", "go")],
}

# CI file locations
_CI_FILES = [
    ".github/workflows/ci.yml",
    ".github/workflows/ci.yaml",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
]

# Doc markers
_DOC_MARKERS: list[tuple[str, str]] = [
    ("README.md", "readme"),
    ("README.rst", "readme"),
    ("docs", "docs_dir"),
    ("doc", "docs_dir"),
]

# Release signal markers
_RELEASE_MARKERS: dict[str, str] = {
    "Dockerfile": "docker",
    "docker-compose.yml": "docker_compose",
    "docker-compose.yaml": "docker_compose",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API ──


def scan_workspace(workspace: str | Path) -> ProjectProfile:
    """Run deterministic scan and return a ProjectProfile with observed facts."""
    ws = Path(workspace).resolve()
    profile = ProjectProfile(
        workspace_root=str(ws),
        workspace_id=workspace_id(ws),
        workspace_type="empty_or_unknown",
        confidence="low",
    )

    # Step 1: classify workspace type
    profile.workspace_type = _classify_workspace(ws)

    # Step 2: repo inventory
    if profile.workspace_type == "single_repo":
        repo = _scan_single_repo(ws, ws)
        if repo:
            profile.repos.append(repo)
    elif profile.workspace_type == "multi_repo":
        profile.repos = _scan_multi_repo(ws)
    elif profile.workspace_type == "plain_directory":
        repo = _scan_plain_dir(ws, ws)
        if repo:
            profile.repos.append(repo)

    # Step 3: test discovery
    profile.test_sources = _discover_test_commands(ws, profile.repos)

    # Step 4: release signals
    profile.release_profile = _detect_release_signals(ws, profile.repos)

    # Step 5: doc assets
    profile.doc_assets = _discover_doc_assets(ws)

    # Step 6: generate observed facts
    profile.facts = _generate_facts(ws, profile)

    # Derive languages from repos
    langs: set[str] = set()
    for repo in profile.repos:
        langs.update(repo.languages)
    profile.languages = sorted(langs)

    # Set confidence
    if profile.repos:
        profile.confidence = "medium"

    return profile


# ── Workspace classification ──


def _classify_workspace(ws: Path) -> str:
    if (ws / ".git").is_dir():
        return "single_repo"

    repos = _find_git_repos(ws, max_depth=2)
    if repos:
        return "multi_repo"

    project_signals = [
        "pom.xml",
        "build.gradle",
        "package.json",
        "pyproject.toml",
        "go.mod",
        "Cargo.toml",
        "setup.py",
        "src",
        "lib",
    ]
    if any((ws / s).exists() for s in project_signals):
        return "plain_directory"

    return "empty_or_unknown"


def _find_git_repos(ws: Path, max_depth: int = 4) -> list[Path]:
    """Find git repos under workspace root."""
    repos: list[Path] = []
    for root, dirs, _files in os.walk(ws):
        rel = Path(root).relative_to(ws)
        if len(rel.parts) > max_depth:
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS and not d.startswith(".")]
        if (Path(root) / ".git").is_dir():
            repos.append(Path(root))
            dirs.clear()
    return repos


# ── Repo scanning ──


def _scan_single_repo(ws: Path, repo_path: Path) -> RepoInfo:
    name = repo_path.name
    relative = str(repo_path.relative_to(ws)) if repo_path != ws else "."
    info = RepoInfo(
        id=name,
        name=name,
        relative_path=relative,
        git_root=str(repo_path),
    )
    _enrich_repo(info, repo_path)
    return info


def _scan_multi_repo(ws: Path) -> list[RepoInfo]:
    repos: list[RepoInfo] = []
    git_repos = _find_git_repos(ws, max_depth=4)
    for gr in sorted(git_repos):
        rel = str(gr.relative_to(ws))
        name = gr.name
        info = RepoInfo(
            id=name,
            name=name,
            relative_path=rel,
            git_root=str(gr),
        )
        _enrich_repo(info, gr)
        repos.append(info)
    return repos


def _scan_plain_dir(ws: Path, repo_path: Path) -> RepoInfo:
    name = repo_path.name
    relative = str(repo_path.relative_to(ws)) if repo_path != ws else "."
    info = RepoInfo(
        id=name,
        name=name,
        relative_path=relative,
        git_root="",
    )
    _detect_build_files(info, repo_path)
    _guess_repo_type(info)
    return info


def _enrich_repo(info: RepoInfo, repo_path: Path) -> None:
    _fill_git_info(info, repo_path)
    _detect_build_files(info, repo_path)
    _guess_repo_type(info)


def _fill_git_info(info: RepoInfo, repo_path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=10,
        )
        if result.returncode == 0:
            info.current_branch = result.stdout.strip()

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=10,
        )
        if result.returncode == 0:
            info.dirty = bool(result.stdout.strip())

        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=10,
        )
        if result.returncode == 0:
            info.remote = result.stdout.strip()

        info.default_branch = _guess_default_branch(repo_path)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    info.evidence.append(
        Evidence(path=str(Path(info.relative_path) / ".git"), kind="git_dir")
    )


def _guess_default_branch(repo_path: Path) -> str:
    """Guess default branch: origin/HEAD -> main -> master -> current."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            ref = result.stdout.strip()
            if ref.startswith("origin/"):
                return ref[len("origin/") :]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    for branch in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                capture_output=True,
                text=True,
                cwd=str(repo_path),
                timeout=10,
            )
            if result.returncode == 0:
                return branch
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "main"


def _detect_build_files(info: RepoInfo, repo_path: Path) -> None:
    for bf, lang in _BUILD_FILE_LANG.items():
        if (repo_path / bf).exists():
            if bf not in info.build_files:
                info.build_files.append(bf)
            if lang not in info.languages:
                info.languages.append(lang)
            info.evidence.append(
                Evidence(path=str(Path(info.relative_path) / bf), kind=bf)
            )

    lang_dirs = {
        "src/main/java": "java",
        "src/main/kotlin": "kotlin",
        "src/main/go": "go",
        "src/main/python": "python",
    }
    for d, lang in lang_dirs.items():
        if (repo_path / d).is_dir() and lang not in info.languages:
            info.languages.append(lang)


def _guess_repo_type(info: RepoInfo) -> None:
    if "package.json" in info.build_files:
        info.repo_type = "frontend"
        return

    backend_langs = {"java", "python", "go", "rust", "ruby", "php"}
    if set(info.languages) & backend_langs:
        info.repo_type = "backend"
        return


# ── Test discovery ──


def _discover_test_commands(ws: Path, repos: list[RepoInfo]) -> list[TestSource]:
    sources: list[TestSource] = []
    seen: set[str] = set()

    for repo in repos:
        repo_path = ws / repo.relative_path
        for bf in repo.build_files:
            for cmd, tool in _TEST_COMMAND_MAP.get(bf, []):
                if cmd in seen:
                    continue
                # Adjust gradle wrapper
                actual_cmd = cmd
                if "./gradlew" in cmd and not (repo_path / "gradlew").exists():
                    actual_cmd = cmd.replace("./gradlew", "gradle")

                seen.add(cmd)
                sources.append(
                    TestSource(
                        id=f"{repo.id}-{tool}",
                        repo_id=repo.id,
                        name=tool,
                        command=actual_cmd,
                        evidence=[
                            Evidence(
                                path=str(Path(repo.relative_path) / bf),
                                kind=bf,
                            )
                        ],
                    )
                )

        for indicator, cmd, tool in [
            ("pytest.ini", "pytest", "pytest"),
            ("tox.ini", "tox", "tox"),
        ]:
            if (repo_path / indicator).exists() and cmd not in seen:
                seen.add(cmd)
                sources.append(
                    TestSource(
                        id=f"{repo.id}-{tool}",
                        repo_id=repo.id,
                        name=tool,
                        command=cmd,
                        evidence=[
                            Evidence(
                                path=str(Path(repo.relative_path) / indicator),
                                kind=indicator,
                            )
                        ],
                    )
                )

    return sources


# ── Release signals ──


def _detect_release_signals(ws: Path, repos: list[RepoInfo]) -> ReleaseProfile:
    signals: list[ReleaseSignal] = []

    if len(repos) > 1:
        signals.append(ReleaseSignal(type="multi_repo_count", value=len(repos)))

    backend_count = sum(1 for r in repos if r.repo_type == "backend")
    frontend_count = sum(1 for r in repos if r.repo_type == "frontend")
    if backend_count and frontend_count:
        signals.append(
            ReleaseSignal(
                type="frontend_backend_split",
                value=f"{frontend_count}f/{backend_count}b",
            )
        )

    for repo in repos:
        repo_path = ws / repo.relative_path
        for marker, sig_type in _RELEASE_MARKERS.items():
            if (repo_path / marker).exists():
                signals.append(ReleaseSignal(type=sig_type, value=repo.id))

    scale = "unknown"
    if len(repos) > 1:
        scale = (
            "frontend_backend" if (backend_count and frontend_count) else "multi_repo"
        )
    elif len(repos) == 1:
        scale = "single_service"

    return ReleaseProfile(
        scale=scale,
        requires_manual_confirm=True,
        signals=signals,
        confirmed=False,
    )


# ── Doc asset discovery ──


def _discover_doc_assets(ws: Path) -> list[DocAsset]:
    assets: list[DocAsset] = []
    for filename, kind in _DOC_MARKERS:
        if (ws / filename).exists():
            assets.append(DocAsset(path=filename, kind=kind))

    for ci_file in _CI_FILES:
        if (ws / ci_file).exists():
            assets.append(DocAsset(path=ci_file, kind="ci_file"))

    return assets


# ── Fact generation ──


def _generate_facts(ws: Path, profile: ProjectProfile) -> list[Fact]:
    facts: list[Fact] = []
    now = _now_iso()

    facts.append(
        Fact(
            id="fact-workspace-type",
            type="workspace_type",
            value=profile.workspace_type,
            scope="workspace",
            source="deterministic_scan",
            confidence="high",
            observed_at=now,
        )
    )

    if profile.repos:
        facts.append(
            Fact(
                id="fact-repo-count",
                type="repo_count",
                value=str(len(profile.repos)),
                scope="workspace",
                source="deterministic_scan",
                confidence="high",
                observed_at=now,
            )
        )

    for repo in profile.repos:
        if repo.languages:
            facts.append(
                Fact(
                    id=f"fact-lang-{repo.id}",
                    type="languages",
                    value=", ".join(repo.languages),
                    scope=f"repo:{repo.id}",
                    source="deterministic_scan",
                    confidence="high",
                    evidence=repo.evidence[:3],
                    observed_at=now,
                )
            )

    for ts in profile.test_sources:
        facts.append(
            Fact(
                id=f"fact-test-{ts.id}",
                type="test_command",
                value=ts.command,
                scope=f"repo:{ts.repo_id}",
                source="deterministic_scan",
                confidence="medium",
                evidence=ts.evidence,
                observed_at=now,
            )
        )

    if profile.release_profile.scale != "unknown":
        facts.append(
            Fact(
                id="fact-release-scale",
                type="release_scale",
                value=profile.release_profile.scale,
                scope="workspace",
                source="deterministic_scan",
                confidence="medium",
                observed_at=now,
            )
        )

    return facts
