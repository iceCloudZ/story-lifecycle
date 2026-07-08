"""Worktree Handler — execute git operations with safety checks.

- prepare_worktrees: create or reuse worktrees for all story projects
- cleanup_worktree: remove a worktree after delivery finalized
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .resolver import resolve_worktrees, WorktreeState
from .decider import (
    PrepareAction,
    CleanupAction,
    RejectReason,
    decide_prepare,
    decide_cleanup,
)


def prepare_worktrees(story_key: str, worktree_root: str = "") -> list[dict]:
    """Prepare worktrees for all project bindings of a story.

    For each story_project binding:
    1. Resolve current git worktree state
    2. Decide: create, reuse, or reject
    3. Execute: git worktree add / git branch if creating

    Returns a list of result dicts with keys:
        story_project: the binding row
        action: create / reuse / reject
        worktree_path: final path (if created/reused)
        error: error message (if rejected/failed)
    """
    from ....infra.db import models as db

    bindings = db.get_story_projects(story_key)
    results: list[dict] = []

    for sp in bindings:
        project = db.get_project(sp["project_id"])
        if not project:
            results.append(
                {
                    "story_project": sp,
                    "action": "reject",
                    "worktree_path": None,
                    "error": f"project {sp['project_id']} not found",
                }
            )
            continue

        repo_path = project["repo_path"]
        if not Path(repo_path).exists():
            results.append(
                {
                    "story_project": sp,
                    "action": "reject",
                    "worktree_path": None,
                    "error": f"project path {repo_path} does not exist",
                }
            )
            continue

        worktrees = resolve_worktrees(repo_path)
        decision = decide_prepare(sp, worktrees)

        if decision.action == PrepareAction.REUSE:
            results.append(
                {
                    "story_project": sp,
                    "action": "reuse",
                    "worktree_path": sp["worktree_path"],
                    "error": None,
                }
            )

        elif decision.action == PrepareAction.CREATE:
            branch = sp.get("branch", "")
            base_branch = sp.get("base_branch", "main")
            base_commit = sp.get("base_commit", "")

            # Determine worktree path (explicit > worktree_root > <repo>/.worktrees/<story>)
            wt_path = _derive_worktree_path(sp, project, story_key, worktree_root)

            # Create branch if it doesn't exist
            try:
                _ensure_branch(repo_path, branch, base_branch, base_commit)
            except Exception as e:
                results.append(
                    {
                        "story_project": sp,
                        "action": "reject",
                        "worktree_path": None,
                        "error": f"failed to create branch {branch}: {e}",
                    }
                )
                continue

            # Create worktree
            try:
                _create_worktree(repo_path, wt_path, branch)
                db.update_story_project(
                    story_key,
                    sp["project_id"],
                    worktree_path=wt_path,
                    worktree_state=WorktreeState.AVAILABLE,
                    workspace_type="worktree",
                )
                results.append(
                    {
                        "story_project": sp,
                        "action": "create",
                        "worktree_path": wt_path,
                        "error": None,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "story_project": sp,
                        "action": "reject",
                        "worktree_path": None,
                        "error": f"failed to create worktree: {e}",
                    }
                )

        else:  # PrepareAction.REJECT
            if decision.reject_reason == RejectReason.PATH_CONFLICT and sp.get(
                "worktree_path"
            ):
                # 显式指定的路径被占(非 worktree)→ 改走外部独立 worktree
                sp_ext = {**sp, "worktree_path": None}
                wt_path = _derive_worktree_path(
                    sp_ext, project, story_key, worktree_root
                )
                branch = sp.get("branch", "")
                base_branch = sp.get("base_branch", "main")
                base_commit = sp.get("base_commit", "")
                try:
                    _ensure_branch(repo_path, branch, base_branch, base_commit)
                    _create_worktree(repo_path, wt_path, branch)
                    db.update_story_project(
                        story_key,
                        sp["project_id"],
                        worktree_path=wt_path,
                        worktree_state=WorktreeState.AVAILABLE,
                        workspace_type="worktree",
                    )
                    results.append(
                        {
                            "story_project": sp,
                            "action": "create_fallback",
                            "worktree_path": wt_path,
                            "error": None,
                        }
                    )
                except Exception as e:
                    results.append(
                        {
                            "story_project": sp,
                            "action": "reject",
                            "worktree_path": None,
                            "error": f"fallback create failed: {e}",
                        }
                    )
            else:
                # NO_BRANCH_NAME / STALE / BRANCH_CHECKED_OUT_ELSEWHERE / ... → 真 reject
                results.append(
                    {
                        "story_project": sp,
                        "action": "reject",
                        "worktree_path": None,
                        "error": decision.reason,
                    }
                )

    return results


def cleanup_worktree(
    story_key: str,
    project_id: int,
    delivery_state: str = "",
    force: bool = False,
) -> dict:
    """Remove a worktree after verifying delivery is finalized.

    P0 constraints:
    - Only removes the worktree (git worktree remove), NOT the branch.
    - Requires delivery_state in (merged, abandoned).
    - Refuses to remove dirty worktrees (unless force=True with caution).
    - Returns a result dict with action and reason.

    Returns:
        dict with keys: action (cleanup/reject), reason, worktree_path
    """
    from ....infra.db import models as db

    sp = db.get_story_project(story_key, project_id)
    if not sp:
        return {
            "action": "reject",
            "reason": f"no story_project binding for {story_key} / {project_id}",
            "worktree_path": None,
        }

    wt_path = sp.get("worktree_path", "")
    if not wt_path:
        return {
            "action": "reject",
            "reason": "no worktree_path set",
            "worktree_path": None,
        }

    wt_path_obj = Path(wt_path)
    worktree_exists = wt_path_obj.exists()

    # Check if worktree is clean
    is_clean = False
    if worktree_exists:
        is_clean = _is_worktree_clean(wt_path)

    decision = decide_cleanup(sp, delivery_state, is_clean, worktree_exists)

    if decision.action == CleanupAction.REJECT:
        return {
            "action": "reject",
            "reason": decision.reason,
            "worktree_path": wt_path,
            "reject_reason": decision.reject_reason.value
            if decision.reject_reason
            else None,
        }

    if not force and not is_clean:
        return {
            "action": "reject",
            "reason": "worktree has uncommitted changes, use force=True to override",
            "worktree_path": wt_path,
            "reject_reason": "worktree_dirty",
        }

    # Execute cleanup
    try:
        # Get the main repo path from the project
        project = db.get_project(project_id)
        repo_path = project["repo_path"] if project else str(wt_path_obj.parent)

        subprocess.run(
            ["git", "worktree", "remove", wt_path],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        # P0: do not delete the branch

        db.update_story_project(
            story_key,
            project_id,
            worktree_path=None,
            worktree_state=WorktreeState.UNPREPARED,
        )

        return {
            "action": "cleanup",
            "reason": f"worktree at {wt_path} removed",
            "worktree_path": wt_path,
        }
    except subprocess.CalledProcessError as e:
        return {
            "action": "reject",
            "reason": f"git worktree remove failed: {e.stderr.strip()}",
            "worktree_path": wt_path,
        }


def _ensure_branch(
    repo_path: str,
    branch: str,
    base_branch: str = "main",
    base_commit: str = "",
) -> None:
    """Create branch if it doesn't exist. Validates branch name first."""
    # Validate branch name
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise ValueError(f"invalid branch name '{branch}': {result.stderr.strip()}")

    # Check if branch already exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 0:
        return  # branch already exists

    # Create branch from base
    start_point = base_commit or base_branch
    subprocess.run(
        ["git", "branch", branch, start_point],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def _create_worktree(repo_path: str, worktree_path: str, branch: str) -> None:
    """Create a git worktree at the given path for the given branch."""
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", worktree_path, branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _is_worktree_clean(worktree_path: str) -> bool:
    """Check if a worktree is clean (no uncommitted changes)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() == ""
    except Exception:
        return False


def _ensure_local_exclude(repo: Path, pattern: str) -> None:
    """把 pattern 加到目标仓 .git/info/exclude(纯本地排除,不改 .gitignore、不 commit)。"""
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as f:
            f.write(f"{pattern}\n")


def _derive_worktree_path(
    sp: dict, project: dict, story_key: str, worktree_root: str
) -> str:
    """决定本次 prepare 用哪个 worktree 路径。
    优先级:绑定显式指定的真实路径 > worktree_root/story_key/project > <repo>/.worktrees/<story_key>。
    不读占位符(已随 NULL 改动消失)。"""
    from ....infra.story_paths import safe_segment

    safe_key = safe_segment(story_key)
    if sp.get("worktree_path"):
        return sp["worktree_path"]
    if worktree_root:
        return str(Path(worktree_root) / safe_key / project["name"])
    repo = Path(project["repo_path"])
    _ensure_local_exclude(repo, ".worktrees/")
    return str(repo / ".worktrees" / safe_key)
