"""Workspace diff — compute a story's git diff via GitLab API or local git.

Extracted from infra/db/models.py (ISS-014, physical layering) to remove the
infra→sourcing upward dependency: models.py is persistence-only and must not
reach up into the sourcing layer for the gitlab integration. Lives in layer
(2) sourcing, so the gitlab integration is a sibling import and the DB reads
(get_story / get_story_projects / get_project) are a clean downward dep on
infra. Single caller: orchestrator/service/api.py.

Prefers GitLab API when GITLAB_TOKEN is configured and the story's project
remote_url points to GitLab; falls back to local ``git diff`` otherwise.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..infra.db.models import get_project, get_story, get_story_projects
from .integrations import gitlab


def _local_git_diff(repo: Path, base_branch: str, current: str) -> dict:
    """Fallback helper: produce a diff dict from local git."""

    def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    diff_range = f"{base_branch}..{current}" if current else base_branch

    try:
        diff_text = _git(["diff", diff_range]).stdout
    except subprocess.CalledProcessError:
        diff_text = ""

    files: list[dict] = []
    # Parse full file paths and line stats from the actual diff output instead of
    # relying on `git diff --stat`, which truncates long paths with ".../".
    current_path = ""
    current_additions = 0
    current_deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            if current_path:
                files.append(
                    {
                        "path": current_path,
                        "additions": current_additions,
                        "deletions": current_deletions,
                        "changes": current_additions + current_deletions,
                    }
                )
            match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
            current_path = match.group(2) if match else line
            current_additions = 0
            current_deletions = 0
        elif line.startswith("+") and not line.startswith("+++"):
            current_additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            current_deletions += 1
    if current_path:
        files.append(
            {
                "path": current_path,
                "additions": current_additions,
                "deletions": current_deletions,
                "changes": current_additions + current_deletions,
            }
        )

    total_additions = sum(f["additions"] for f in files)
    total_deletions = sum(f["deletions"] for f in files)

    return {
        "diff_range": diff_range,
        "files": files,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "total_changes": total_additions + total_deletions,
        "diff": diff_text,
        "is_empty": not diff_text.strip(),
    }


def get_story_workspace_diff(story_key: str) -> dict:
    """Return git diff between a story's workspace branch and its base branch.

    Prefers GitLab API when GITLAB_TOKEN is configured and the story's project
    remote_url points to GitLab. Falls back to local ``git diff`` otherwise.
    """

    story = get_story(story_key)
    if not story:
        raise ValueError(f"story not found: {story_key}")

    workspace = story.get("workspace", "")
    if not workspace or not Path(workspace).exists():
        raise ValueError(f"invalid workspace: {workspace}")

    repo = Path(workspace)
    # If the workspace itself is not a git repo, try the first bound project path.
    if not (repo / ".git").exists():
        for sp in get_story_projects(story_key):
            proj = get_project(sp.get("project_id"))
            if not proj:
                continue
            candidate = Path(proj.get("repo_path", ""))
            if candidate.exists() and (candidate / ".git").exists():
                repo = candidate
                break

    if not (repo / ".git").exists():
        raise ValueError(f"workspace is not a git repository: {workspace}")

    def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    # Current branch
    try:
        current = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    except subprocess.CalledProcessError:
        current = ""

    # Base branch: prefer story_project binding, then repo default, then main/master.
    base_branch = ""
    source_branch = ""
    for sp in get_story_projects(story_key):
        if sp.get("branch"):
            source_branch = sp["branch"]
        if sp.get("base_branch"):
            base_branch = sp["base_branch"]
            break

    if not base_branch:
        try:
            sym = _git(["symbolic-ref", "refs/remotes/origin/HEAD"]).stdout.strip()
            if sym.startswith("refs/remotes/origin/"):
                base_branch = sym.split("/")[-1]
        except subprocess.CalledProcessError:
            pass

    if not base_branch:
        for candidate in ("main", "master"):
            try:
                _git(["rev-parse", "--verify", candidate])
                base_branch = candidate
                break
            except subprocess.CalledProcessError:
                continue

    if not base_branch:
        base_branch = "HEAD"

    # Try GitLab first when a token is present.
    gitlab_result = _try_gitlab_diff(
        story_key, repo, source_branch or current, base_branch
    )
    if gitlab_result:
        return gitlab_result

    # Fallback to local git diff.
    local = _local_git_diff(repo, base_branch, current)
    return {
        "source": "local",
        "current_branch": current,
        "base_branch": base_branch,
        "mr_iid": None,
        "mr_url": "",
        "gitlab_url": "",
        **local,
    }


def _try_gitlab_diff(
    story_key: str, repo: Path, source_branch: str, base_branch: str
) -> dict | None:
    """Attempt to fetch the diff from GitLab API. Returns None on any failure."""

    if not gitlab._token():
        return None

    # Try story_project bindings first; if none, fall back to repo path match.
    project = None
    bound_projects = get_story_projects(story_key)
    for sp in bound_projects:
        proj = get_project(sp.get("project_id"))
        if proj and proj.get("remote_url"):
            project = proj
            break

    if not project:
        # Fallback: find a registered project whose repo_path contains this repo.
        for sp in bound_projects:
            proj = get_project(sp.get("project_id"))
            if not proj:
                continue
            proj_path = Path(proj.get("repo_path", ""))
            if proj_path.exists() and (repo == proj_path or proj_path in repo.parents):
                project = proj
                break

    if not project:
        return None

    remote_url = project.get("remote_url", "")
    project_path = gitlab.parse_project_path(remote_url)
    if not project_path:
        return None

    gl_project = gitlab.get_project(project_path)
    if not gl_project:
        return None

    gl_project_id = gl_project.get("id")
    if not gl_project_id:
        return None

    mr = gitlab.find_merge_request(gl_project_id, source_branch)
    if not mr:
        return None

    mr_iid = mr.get("iid")
    changes_data = gitlab.get_mr_changes(gl_project_id, mr_iid)
    if not changes_data:
        return None

    files = []
    diff_parts = []
    for change in changes_data.get("changes", []):
        new_path = change.get("new_path", "")
        old_path = change.get("old_path", "")
        diff_text = change.get("diff", "")

        # GitLab /changes returns hunks without the git file header.
        # Reconstruct a valid unified-diff header so diff2html can render it.
        is_new = change.get("new_file", False)
        is_deleted = change.get("deleted_file", False)
        display_path = new_path or old_path

        header_lines = [f"diff --git a/{old_path or new_path} b/{new_path or old_path}"]
        if is_new:
            header_lines.append("new file mode 100644")
            header_lines.append("--- /dev/null")
            header_lines.append(f"+++ b/{new_path}")
        elif is_deleted:
            header_lines.append("deleted file mode 100644")
            header_lines.append(f"--- a/{old_path}")
            header_lines.append("+++ /dev/null")
        else:
            header_lines.append(f"--- a/{old_path}")
            header_lines.append(f"+++ b/{new_path}")

        header = "\n".join(header_lines) + "\n"
        full_file_diff = header + diff_text + "\n"
        diff_parts.append(full_file_diff)

        # GitLab /changes endpoint does not always include additions/deletions,
        # so estimate from the diff text hunk headers.
        additions = 0
        deletions = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        files.append(
            {
                "path": display_path,
                "old_path": old_path,
                "new_path": new_path,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
                "new_file": is_new,
                "deleted_file": is_deleted,
                "renamed_file": change.get("renamed_file", False),
            }
        )

    total_additions = sum(f["additions"] for f in files)
    total_deletions = sum(f["deletions"] for f in files)

    return {
        "source": "gitlab",
        "current_branch": source_branch,
        "base_branch": changes_data.get("target_branch") or base_branch,
        "diff_range": f"{base_branch}...{source_branch}",
        "mr_iid": mr_iid,
        "mr_url": gitlab.build_mr_url(project_path, mr_iid),
        "gitlab_url": gitlab._gitlab_url(),
        "project_path": project_path,
        "files": files,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "total_changes": total_additions + total_deletions,
        "diff": "".join(diff_parts),
        "is_empty": not diff_parts,
    }
