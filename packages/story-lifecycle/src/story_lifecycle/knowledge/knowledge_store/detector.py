"""Deterministic project structure detection.

Scans the filesystem to identify services, frontends, docs, tests,
and existing knowledge — without requiring any AI or external tool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Directories that are always ignored during detection
IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "target",
        ".gradle",
        ".mvn",
        "build",
        "dist",
        ".umi",
        ".umi-production",
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

# File extensions that indicate generated/dependency content
_IGNORED_EXTENSIONS_IN_STATS = frozenset(
    {
        ".class",
        ".jar",
        ".war",
        ".pyc",
        ".pyo",
    }
)

# Markers that identify a directory as a Java/Spring service
_JAVA_SERVICE_MARKERS = frozenset(
    {
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }
)

# Markers that identify a directory as a Spring Boot application
_SPRING_BOOT_MARKERS = frozenset(
    {
        "src/main/resources/application.yml",
        "src/main/resources/application.yaml",
        "src/main/resources/application.properties",
    }
)

# Markers that identify a frontend app
_FRONTEND_MARKERS = frozenset(
    {
        "package.json",
    }
)

# Known frontend framework dirs (inside a frontend app)
_FRONTEND_FRAMEWORK_DIRS = frozenset(
    {
        "src",
        "pages",
        "components",
        "routes",
    }
)

# Doc file names / dirs
_DOC_DIRS = frozenset(
    {
        "docs",
        "doc",
        "documentation",
    }
)

_SPEC_DIRS = frozenset(
    {
        "prd",
        "spec",
        "specs",
        "plans",
        "design",
    }
)

_BUG_DIRS = frozenset(
    {
        "bugs",
        "bug-records",
        "issues",
    }
)


@dataclass
class ServiceInfo:
    id: str
    path: str
    type: str  # e.g. "java-spring-service", "python-service"
    included: bool = True
    reason: str = ""


@dataclass
class FrontendInfo:
    id: str
    path: str
    type: str = "frontend-app"
    included: bool = False
    reason: str = ""


@dataclass
class DetectionResult:
    root: str
    product_guess: str
    services: list[ServiceInfo] = field(default_factory=list)
    frontends: list[FrontendInfo] = field(default_factory=list)
    doc_dirs: list[str] = field(default_factory=list)
    spec_dirs: list[str] = field(default_factory=list)
    bug_dirs: list[str] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    ignored_or_generated: list[str] = field(default_factory=list)
    existing_knowledge: bool = False
    codegraph_cache: bool = False
    warnings: list[str] = field(default_factory=list)
    # File stats by extension
    file_stats: dict[str, int] = field(default_factory=dict)


def detect_project(workspace: str | Path) -> DetectionResult:
    """Run deterministic filesystem detection on the workspace."""
    ws = Path(workspace).resolve()
    root_name = ws.name
    product_guess = _guess_product_name(root_name)

    result = DetectionResult(
        root=str(ws),
        product_guess=product_guess,
    )

    # Check existing state
    result.existing_knowledge = (ws / ".story" / "knowledge" / "manifest.yaml").exists()
    result.codegraph_cache = (ws / ".codegraph").exists()

    # Scan top-level directories
    _scan_top_level(ws, result)

    # Collect ignored/generated paths
    _collect_ignored(ws, result)

    # Count files by extension (excluding ignored dirs)
    result.file_stats = _count_files_by_ext(ws)

    # Diagnose: Java service dirs but no Java files
    _check_java_diagnostic(result)

    return result


def _guess_product_name(root_name: str) -> str:
    """Guess product name from directory name.

    Examples: 'hc-all' -> 'happycash', 'my-project' -> 'my-project'
    """
    # Common abbreviation patterns
    abbrevs = {"hc": "happycash"}
    parts = root_name.lower().replace("_", "-").split("-")
    resolved = [abbrevs.get(p, p) for p in parts]
    # Drop generic suffixes like "all", "project", "workspace"
    generic = {"all", "project", "workspace", "repo", "code", "src"}
    filtered = [p for p in resolved if p not in generic]
    return "-".join(filtered) if filtered else root_name.lower()


def _scan_top_level(ws: Path, result: DetectionResult) -> None:
    """Scan immediate children of workspace to classify directories."""
    try:
        entries = sorted(ws.iterdir())
    except PermissionError:
        result.warnings.append(f"Cannot read directory: {ws}")
        return

    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        # Skip hidden and ignored dirs
        if name.startswith(".") or name in IGNORED_DIRS:
            continue

        # Classify
        if _is_java_spring_service(entry):
            result.services.append(
                ServiceInfo(
                    id=name,
                    path=name,
                    type="java-spring-service",
                    included=True,
                    reason="core service",
                )
            )
        elif _is_python_service(entry):
            result.services.append(
                ServiceInfo(
                    id=name,
                    path=name,
                    type="python-service",
                    included=True,
                    reason="core service",
                )
            )
        elif _is_frontend_app(entry):
            result.frontends.append(
                FrontendInfo(
                    id=name,
                    path=name,
                    reason="excluded from P0 to reduce noise",
                )
            )
        elif name.lower() in _DOC_DIRS:
            result.doc_dirs.append(name)
        elif name.lower() in _SPEC_DIRS:
            result.spec_dirs.append(name)
        elif name.lower() in _BUG_DIRS:
            result.bug_dirs.append(name)
        elif name.lower() in ("test", "tests", "testing", "__tests__"):
            result.test_dirs.append(name)
        # Check if it's a multi-service container (has services inside)
        elif _is_frontend_container(entry):
            _scan_nested_services(entry, result)
        elif _is_service_container(entry):
            _scan_nested_services(entry, result)


def _is_java_spring_service(path: Path) -> bool:
    """Check if a directory looks like a Java/Spring service."""
    has_build = any((path / m).exists() for m in _JAVA_SERVICE_MARKERS)
    if not has_build:
        return False
    # Also check for Spring Boot markers or src/main/java
    has_spring = any((path / m).exists() for m in _SPRING_BOOT_MARKERS)
    has_java_src = (path / "src" / "main" / "java").is_dir()
    return has_spring or has_java_src


def _is_python_service(path: Path) -> bool:
    """Check if a directory looks like a Python service."""
    has_setup = (path / "setup.py").exists() or (path / "pyproject.toml").exists()
    has_src = (path / "src").is_dir() and any((path / "src").glob("*/*.py"))
    return has_setup or has_src


def _is_frontend_app(path: Path) -> bool:
    """Check if a directory looks like a frontend application."""
    pkg = path / "package.json"
    if not pkg.exists():
        return False
    # Must have src/ or similar frontend dirs
    return any((path / d).is_dir() for d in _FRONTEND_FRAMEWORK_DIRS)


def _is_frontend_container(path: Path) -> bool:
    """Check if a named container directory holds frontend apps.

    E.g. 'frontends/' with even a single frontend child.
    """
    container_names = {"frontends", "frontend", "web", "web-apps", "apps"}
    if path.name.lower() not in container_names:
        return False
    children = [c for c in path.iterdir() if c.is_dir() and not c.name.startswith(".")]
    return any(_is_frontend_app(c) for c in children)


def _is_service_container(path: Path) -> bool:
    """Check if a directory contains multiple service subdirectories.

    E.g. 'frontends/' contains hc-admin, hc-mobile, etc.
    """
    # A container has multiple child dirs that look like apps/services
    children = [c for c in path.iterdir() if c.is_dir() and not c.name.startswith(".")]
    if len(children) < 2:
        return False
    # Check if at least 2 children are identifiable apps/services
    identified = 0
    for child in children:
        if (child / "package.json").exists():
            identified += 1
        elif any((child / m).exists() for m in _JAVA_SERVICE_MARKERS):
            identified += 1
    return identified >= 2


def _scan_nested_services(parent: Path, result: DetectionResult) -> None:
    """Scan a container directory for nested services/frontends."""
    for child in sorted(parent.iterdir()):
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or child.name in IGNORED_DIRS
        ):
            continue
        rel = f"{parent.name}/{child.name}"
        if _is_java_spring_service(child):
            result.services.append(
                ServiceInfo(
                    id=child.name,
                    path=rel,
                    type="java-spring-service",
                    included=True,
                    reason="core service",
                )
            )
        elif _is_frontend_app(child):
            result.frontends.append(
                FrontendInfo(
                    id=child.name,
                    path=rel,
                    reason="excluded from P0 to reduce noise",
                )
            )


def _collect_ignored(ws: Path, result: DetectionResult) -> None:
    """Collect known generated/dependency directories for reporting."""
    for d in sorted(ws.iterdir()):
        if not d.is_dir():
            continue
        if d.name in IGNORED_DIRS or d.name.startswith("."):
            result.ignored_or_generated.append(d.name)
        elif _is_frontend_app(d):
            nm = d / "node_modules"
            if nm.is_dir():
                result.ignored_or_generated.append(f"{d.name}/node_modules")
            dist = d / "dist"
            if dist.is_dir():
                result.ignored_or_generated.append(f"{d.name}/dist")
    # Also check nested frontend containers
    for fe in result.frontends:
        fe_path = ws / fe.path
        nm = fe_path / "node_modules"
        if nm.is_dir() and f"{fe.path}/node_modules" not in result.ignored_or_generated:
            result.ignored_or_generated.append(f"{fe.path}/node_modules")


def _count_files_by_ext(ws: Path) -> dict[str, int]:
    """Count source files by extension, skipping ignored dirs."""
    ext_map: dict[str, int] = {}
    # Map extensions to display groups
    ext_groups = {
        ".java": "java",
        ".xml": "xml",
        ".yaml": "yaml/yml",
        ".yml": "yaml/yml",
        ".sql": "sql",
        ".ts": "ts/tsx",
        ".tsx": "ts/tsx",
        ".js": "js/jsx",
        ".jsx": "js/jsx",
        ".py": "python",
        ".kt": "kotlin",
        ".go": "go",
        ".vue": "vue",
        ".css": "css",
        ".scss": "css",
        ".less": "css",
        ".html": "html",
        ".json": "json",
        ".md": "markdown",
        ".properties": "properties",
    }

    for root, dirs, files in os.walk(ws):
        # Prune ignored directories in-place
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]

        for f in files:
            ext = Path(f).suffix.lower()
            if ext in _IGNORED_EXTENSIONS_IN_STATS:
                continue
            group = ext_groups.get(ext)
            if group:
                ext_map[group] = ext_map.get(group, 0) + 1

    return ext_map


def _check_java_diagnostic(result: DetectionResult) -> None:
    """Warn if Java service dirs exist but no Java files were found."""
    has_java_services = any(s.type == "java-spring-service" for s in result.services)
    java_count = result.file_stats.get("java", 0)
    if has_java_services and java_count == 0:
        result.warnings.append(
            "Java service directories detected, but no Java files counted. "
            "Possible causes: .gitignore excluded service directories, "
            "scan scope issue, or source not present."
        )
