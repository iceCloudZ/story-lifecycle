"""Project state probe — detect what phase a project is in."""

from __future__ import annotations

from pathlib import Path


class ProjectPhase:
    EMPTY = "empty"  # No git repo, no code, just an idea
    HAS_CODE_NO_PLAN = "has_code_no_plan"  # Has repo/code but no planning files
    HAS_REQUIREMENTS = "has_requirements"  # Has requirements.md but no roadmap
    HAS_ROADMAP = "has_roadmap"  # Has roadmap.md but not decomposed
    HAS_ISSUES = "has_issues"  # Roadmap + issues all present


def probe_project(cwd: str | Path | None = None) -> dict:
    """Probe the current directory and return project state.

    Returns:
        {
            "phase": str (one of ProjectPhase constants),
            "signals": dict (what was detected),
            "suggested_step": str (next step to run),
        }
    """
    root = Path(cwd) if cwd else Path.cwd()
    signals = {
        "has_git": (root / ".git").is_dir(),
        "has_story_dir": (root / ".story").is_dir(),
        "has_code": bool(_find_code_files(root)),
        "has_planning_dir": (root / ".story" / "planning").is_dir(),
        "has_requirements": (
            root / ".story" / "planning" / "requirements.md"
        ).is_file(),
        "has_roadmap": (root / ".story" / "planning" / "roadmap.md").is_file(),
        "has_issues_json": (root / ".story" / "planning" / "issues.json").is_file(),
    }

    if not signals["has_git"] and not signals["has_code"]:
        return _result(ProjectPhase.EMPTY, signals, "step_0a")
    if signals["has_issues_json"]:
        return _result(ProjectPhase.HAS_ISSUES, signals, "execute")
    if signals["has_roadmap"]:
        return _result(ProjectPhase.HAS_ROADMAP, signals, "step_2")
    if signals["has_requirements"]:
        return _result(ProjectPhase.HAS_REQUIREMENTS, signals, "step_1")
    return _result(ProjectPhase.HAS_CODE_NO_PLAN, signals, "step_1")


def _result(phase: str, signals: dict, suggested_step: str) -> dict:
    return {"phase": phase, "signals": signals, "suggested_step": suggested_step}


_CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".js",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".cpp",
    ".c",
    ".cs",
}


def _find_code_files(root: Path, max_depth: int = 3) -> list[Path]:
    """Find code files up to max_depth, exclude .git, node_modules, venv."""
    results = []
    skip = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        "dist",
        "build",
    }
    try:
        for item in root.rglob("*"):
            if any(skip_dir in item.parts for skip_dir in skip):
                continue
            if len(item.relative_to(root).parts) > max_depth:
                continue
            if item.suffix in _CODE_EXTENSIONS:
                results.append(item)
                if len(results) >= 5:
                    break
    except PermissionError:
        pass
    return results
