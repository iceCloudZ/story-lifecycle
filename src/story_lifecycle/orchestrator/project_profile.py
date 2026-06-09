"""Project Profile — repo scanner that produces a project profile.

Scans a workspace to identify: languages, package managers, entry files,
test directories, CI files. Produces `.story/project/profile.json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class ProjectProfile:
    """Observed facts about a project workspace."""

    workspace: str = ""
    languages: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    entry_files: list[str] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    ci_files: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def scan_workspace(workspace: str) -> ProjectProfile:
    """Deterministic scan of a workspace to generate observed facts."""
    ws = Path(workspace)
    profile = ProjectProfile(workspace=workspace)

    # Language detection by file extensions
    ext_langs = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "react",
        ".tsx": "react",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".cs": "csharp",
        ".cpp": "cpp",
        ".c": "c",
    }
    lang_counts: dict[str, int] = {}
    for ext, lang in ext_langs.items():
        count = len(list(ws.rglob(f"*{ext}")))
        if count > 0:
            lang_counts[lang] = count
    profile.languages = sorted(
        lang_counts.keys(), key=lambda lang: lang_counts[lang], reverse=True
    )

    # Package manager detection
    pm_files = {
        "pyproject.toml": "pip/poetry",
        "setup.py": "pip",
        "requirements.txt": "pip",
        "package.json": "npm",
        "pnpm-lock.yaml": "pnpm",
        "yarn.lock": "yarn",
        "Cargo.toml": "cargo",
        "go.mod": "go_modules",
        "pom.xml": "maven",
        "build.gradle": "gradle",
        "Gemfile": "bundler",
        "composer.json": "composer",
    }
    for pm_file, pm_name in pm_files.items():
        if (ws / pm_file).exists():
            profile.package_managers.append(pm_name)

    # Entry file detection
    entry_candidates = [
        "main.py",
        "app.py",
        "index.py",
        "src/__main__.py",
        "src/main.py",
        "index.ts",
        "index.js",
        "main.go",
        "Main.java",
    ]
    for ec in entry_candidates:
        if (ws / ec).exists():
            profile.entry_files.append(ec)

    # Test directory detection
    test_dirs_candidates = ["tests", "test", "spec", "__tests__", "src/test"]
    for td in test_dirs_candidates:
        if (ws / td).is_dir():
            profile.test_dirs.append(td)

    # Test command inference
    if (
        (ws / "pyproject.toml").exists()
        or (ws / "pytest.ini").exists()
        or (ws / "conftest.py").exists()
    ):
        profile.test_commands.append("pytest")
    if (ws / "package.json").exists():
        profile.test_commands.append("npm test")
    if (ws / "pom.xml").exists():
        profile.test_commands.append("mvn test")

    # CI file detection
    ci_paths = [
        ".github/workflows/ci.yml",
        ".github/workflows/ci.yaml",
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "Jenkinsfile",
    ]
    for ci in ci_paths:
        if (ws / ci).exists():
            profile.ci_files.append(ci)

    # Framework detection
    if any((ws / p).exists() for p in ["fastapi", "app/fastapi"]):
        pass  # too generic
    if (ws / "pyproject.toml").exists():
        content = (ws / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
        if "fastapi" in content.lower():
            profile.frameworks.append("fastapi")
        if "django" in content.lower():
            profile.frameworks.append("django")
        if "flask" in content.lower():
            profile.frameworks.append("flask")
    if (ws / "package.json").exists():
        content = (ws / "package.json").read_text(encoding="utf-8", errors="ignore")
        if "react" in content.lower():
            profile.frameworks.append("react")
        if "next" in content.lower():
            profile.frameworks.append("next.js")
        if "vue" in content.lower():
            profile.frameworks.append("vue")

    return profile


def save_profile(workspace: str, profile: ProjectProfile) -> None:
    """Save project profile to .story/project/profile.json."""
    profile_path = Path(workspace) / ".story" / "project" / "profile.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(asdict(profile), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_profile(workspace: str) -> ProjectProfile | None:
    """Load project profile from .story/project/profile.json."""
    profile_path = Path(workspace) / ".story" / "project" / "profile.json"
    if not profile_path.exists():
        return None
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        return ProjectProfile(**data)
    except (json.JSONDecodeError, TypeError):
        return None
