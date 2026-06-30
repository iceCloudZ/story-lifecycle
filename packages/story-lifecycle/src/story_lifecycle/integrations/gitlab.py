"""GitLab API client for fetching merge request diffs.

Token and base URL are read from environment:
    GITLAB_TOKEN   - Personal/Project access token with read_api + read_repository
    GITLAB_URL     - Optional base URL, defaults to internal self-hosted instance.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

log = logging.getLogger("story-lifecycle.gitlab")

DEFAULT_GITLAB_URL = "http://internal-gitlab.yinshantech.cn"


def _gitlab_url() -> str:
    return os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL).rstrip("/")


def _token() -> str | None:
    return os.environ.get("GITLAB_TOKEN") or None


def _client() -> httpx.Client:
    token = _token()
    headers = {"PRIVATE-TOKEN": token} if token else {}
    return httpx.Client(base_url=_gitlab_url(), headers=headers, timeout=30)


def parse_project_path(remote_url: str) -> str | None:
    """Extract 'group/project' path from common GitLab remote URL forms.

    Supported forms:
        ssh://git@host:port/group/project.git
        http(s)://host/group/project.git
        git@host:group/project.git
    """
    if not remote_url:
        return None

    url = remote_url.strip()
    # Strip trailing .git
    if url.endswith(".git"):
        url = url[:-4]

    # ssh://git@host:port/group/project
    if url.startswith("ssh://"):
        # ssh://git@host:port/group/project
        rest = url[6:]  # git@host:port/group/project
        if "/" in rest:
            # Find first '/' after host:port
            path_start = rest.find("/")
            return rest[path_start + 1 :]
        return None

    # git@host:group/project
    if ":" in url and "@" in url and not url.startswith("http"):
        path_part = url.split(":", 1)[1]
        return path_part if "/" in path_part else None

    # http(s)://host/group/project
    if url.startswith("http://") or url.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        return path if "/" in path else None

    return None


def get_project(remote_path: str) -> dict | None:
    """Lookup a GitLab project by 'group/project' path."""
    token = _token()
    if not token:
        return None

    encoded = quote(remote_path, safe="")
    with _client() as client:
        try:
            r = client.get(f"/api/v4/projects/{encoded}")
            if r.status_code == 200:
                return r.json()
            log.warning("gitlab project lookup failed: %s %s", r.status_code, remote_path)
        except Exception as e:
            log.warning("gitlab project lookup error: %s", e)
    return None


def find_merge_request(project_id: int, source_branch: str) -> dict | None:
    """Find an MR by source branch. Prefers opened, falls back to any state."""
    token = _token()
    if not token:
        return None

    with _client() as client:
        for state in ("opened", "all"):
            try:
                r = client.get(
                    f"/api/v4/projects/{project_id}/merge_requests",
                    params={"source_branch": source_branch, "state": state, "per_page": "10"},
                )
                if r.status_code != 200:
                    continue
                mrs = r.json()
                if mrs:
                    # Prefer first opened MR
                    for mr in mrs:
                        if mr.get("state") == "opened":
                            return mr
                    return mrs[0]
            except Exception as e:
                log.warning("gitlab mr search error: %s", e)
    return None


def get_mr_changes(project_id: int, mr_iid: int) -> dict | None:
    """Return MR metadata + changes from GitLab API."""
    token = _token()
    if not token:
        return None

    with _client() as client:
        try:
            r = client.get(f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes")
            if r.status_code == 200:
                return r.json()
            log.warning("gitlab mr changes failed: %s", r.status_code)
        except Exception as e:
            log.warning("gitlab mr changes error: %s", e)
    return None


def build_compare_url(project_path: str, base_branch: str, source_branch: str) -> str:
    """Build a GitLab branch compare URL for manual fallback."""
    return (
        f"{_gitlab_url()}/{project_path}/-/compare/{base_branch}...{source_branch}"
    )


def build_mr_url(project_path: str, mr_iid: int) -> str:
    """Build a GitLab MR URL."""
    return f"{_gitlab_url()}/{project_path}/-/merge_requests/{mr_iid}"
