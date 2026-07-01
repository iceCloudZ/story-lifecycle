"""Project registry & runtime facts — wraps db CRUD with business logic.

Provides:
- Path normalization (Path.resolve()) before saving
- Availability detection via git rev-parse --is-inside-work-tree
- Runtime fact recording via db.upsert_runtime_facts
"""

import subprocess
from pathlib import Path

from ...db import models as db


def register_project(
    name: str,
    repo_path: str,
    default_branch: str = "main",
    remote_url: str = "",
) -> dict:
    """Register a project. Normalizes repo_path, checks path existence.

    Returns the created project dict.
    """
    if not name or not name.strip():
        raise ValueError("project name must not be empty")
    if not repo_path or not repo_path.strip():
        raise ValueError("repo_path must not be empty")

    resolved = Path(repo_path).resolve()
    repo_path_str = str(resolved)

    if not resolved.exists():
        return db.create_project(
            name=name.strip(),
            repo_path=repo_path_str,
            default_branch=default_branch,
            remote_url=remote_url,
            availability="missing",
            availability_reason=f"Path does not exist: {repo_path_str}",
        )

    return db.create_project(
        name=name.strip(),
        repo_path=repo_path_str,
        default_branch=default_branch,
        remote_url=remote_url,
    )


def check_project_availability(project_id: int) -> dict | None:
    """Check if a project's repo_path is a valid git worktree.

    Updates the project's availability field and returns the updated project dict.
    Returns None if the project does not exist.
    """
    project = db.get_project(project_id)
    if project is None:
        return None

    repo_path = Path(project["repo_path"])

    if not repo_path.exists():
        db.update_project(
            project_id,
            availability="missing",
            availability_reason=f"Path does not exist: {repo_path}",
        )
        return db.get_project(project_id)

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Inside a git worktree (root or subdirectory) — both are fine
            db.update_project(
                project_id,
                availability="available",
                availability_reason="",
            )
        else:
            error_msg = (
                result.stderr.strip()
                if result.stderr
                else "not inside a git repository"
            )
            db.update_project(
                project_id,
                availability="unavailable",
                availability_reason=error_msg,
            )
    except subprocess.TimeoutExpired:
        db.update_project(
            project_id,
            availability="unknown",
            availability_reason="git rev-parse timed out",
        )
    except FileNotFoundError:
        db.update_project(
            project_id,
            availability="unavailable",
            availability_reason="git command not found",
        )
    except Exception as e:
        db.update_project(
            project_id,
            availability="unknown",
            availability_reason=f"git check failed: {e}",
        )

    return db.get_project(project_id)


def list_projects() -> list[dict]:
    """Return all registered projects."""
    return db.list_projects()


def get_project(project_id: int) -> dict | None:
    """Get a single project by id."""
    return db.get_project(project_id)


def update_project(project_id: int, **kwargs) -> None:
    """Update project fields. Normalizes repo_path if provided."""
    if "repo_path" in kwargs:
        kwargs["repo_path"] = str(Path(kwargs["repo_path"]).resolve())
    db.update_project(project_id, **kwargs)


def add_runtime_fact(
    project_id: int,
    runtime_type: str,
    runtime_version: str = "",
    dependency_ref: str = "",
    check_command: str = "",
    availability: str = "unknown",
    evidence_ref: str = "",
) -> dict:
    """Record a runtime fact for a project.

    Uses db.upsert_runtime_facts — one row per (project_id, runtime_type).
    Returns the upserted row.
    """
    return db.upsert_runtime_facts(
        project_id=project_id,
        runtime_type=runtime_type,
        runtime_version=runtime_version,
        dependency_ref=dependency_ref,
        check_command=check_command,
        availability=availability,
        evidence_ref=evidence_ref,
    )
