# src/story_lifecycle/knowledge/stale.py
"""检测知识包是否过期（stale）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import manifest_path


def _get_git_commit(workspace: str | Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(workspace),
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def check_stale(workspace: str | Path) -> dict:
    """返回 {"stale": bool, "reason": str, "commit": str|None}。"""
    import yaml

    mp = manifest_path(workspace)

    if not mp.exists():
        return {"stale": True, "reason": "manifest.yaml 不存在", "commit": None}

    try:
        data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    except Exception as e:
        return {"stale": True, "reason": f"manifest 解析失败: {e}", "commit": None}

    if not isinstance(data, dict):
        return {"stale": True, "reason": "manifest 格式错误", "commit": None}

    if data.get("status") == "stale":
        return {
            "stale": True,
            "reason": "manifest 状态已标记为 stale",
            "commit": data.get("source", {}).get("commit"),
        }

    source = data.get("source", {})
    saved_commit = source.get("commit", "")
    current_commit = _get_git_commit(workspace)

    if current_commit and saved_commit and current_commit != saved_commit:
        return {
            "stale": True,
            "reason": f"commit 变化: {saved_commit[:12]} → {current_commit[:12]}",
            "commit": current_commit,
        }

    return {"stale": False, "reason": "", "commit": current_commit or saved_commit}
