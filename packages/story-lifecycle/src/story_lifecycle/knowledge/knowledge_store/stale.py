# src/story_lifecycle/knowledge/stale.py
"""检测知识包是否过期（stale）。

设计 10 改动 4.3（修订点 R5b）：scenario 级 stale 检测不用 ``stat().st_mtime``
（git checkout / 分支切换 / 编辑器 touch 都会刷 mtime，误报率极高），改用
``git log -1 --format=%ct -- <path>`` 拿文件的真实最后 git 变更时间。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import knowledge_dir, manifest_path


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


def _git_last_change_ts(workspace: str | Path, rel_path: str) -> int | None:
    """文件最后 git 变更时间（epoch 秒）。

    用 ``git log -1 --format=%ct -- <path>`` 而非 mtime——checkout/touch 会刷
    mtime 造成误报（修订点 R5b）。路径不在 git 跟踪内/非 git 仓库 → None。
    """
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(rel_path)],
            capture_output=True,
            text=True,
            cwd=str(workspace),
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip())
    except Exception:
        pass
    return None


def _parse_time(value: str) -> int:
    """把 verified_at（ISO 或 epoch 字符串）解析为 epoch 秒；解析失败 → 0。"""
    if not value:
        return 0
    v = str(value).strip()
    try:
        return int(v)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def load_index_entries(workspace: str | Path) -> list:
    """加载 <workspace>/.story/knowledge 的 INDEX.json 条目（best-effort）。

    knowledge 包未装 / 目录不存在 / INDEX 生成失败 → 空列表（不阻断 stale 检测）。
    """
    try:
        from knowledge import KnowledgeIndex

        idx = KnowledgeIndex(str(knowledge_dir(workspace)))
        return idx.all()
    except Exception:
        return []


def check_stale(workspace: str | Path) -> dict:
    """返回 {"stale": bool, "reason": str, "commit": str|None}。

    两层检测：
    1. 现有 git commit 比对（manifest source.commit vs HEAD）——保留。
    2. scenario 级（设计 10 改动 4.3）：
       - source_refs 代码文件的最后 git 变更时间 > verified_at → 代码已变更
       - last_status == "FAIL" → 绑定的 journey 最近失败
    命中则返回 {"stale": True, "scenarios": [...]}，reason 带过期场景数。
    """
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

    # scenario 级 stale:代码最后 git 变更 vs 人工确认时间 / journey 最近失败
    scenarios_stale = []
    for entry in load_index_entries(workspace):
        if getattr(entry, "type", "") != "scenario":
            continue
        reasons = []
        for ref in entry.source_refs or []:
            code_file = Path(workspace) / ref
            if not code_file.exists():
                continue
            code_ts = _git_last_change_ts(workspace, ref)
            verified = _parse_time(entry.verified_at or "")
            if code_ts and verified and code_ts > verified:
                reasons.append(f"代码变更: {ref}")
        if (entry.last_status or "") == "FAIL":
            reasons.append("绑定的 journey 最近失败")
        if reasons:
            scenarios_stale.append({"id": entry.id, "reasons": reasons})

    if scenarios_stale:
        return {
            "stale": True,
            "reason": f"{len(scenarios_stale)} 个 scenario 过期",
            "scenarios": scenarios_stale,
            "commit": current_commit,
        }

    return {"stale": False, "reason": "", "commit": current_commit or saved_commit}
