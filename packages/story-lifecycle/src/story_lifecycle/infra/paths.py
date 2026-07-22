"""Workspace path registry — single source of truth for .story/ layout.

All runtime code must use these helpers instead of hand-building paths
like ``Path(workspace) / ".story-done" / ...``.  The on-disk layout:

    .story/
      done/          stage handshake files (was .story-done)
      context/       plans, reviews, packets (was .story-context)
      runs/          benchmark run workspaces (was .story-runs)
"""

from __future__ import annotations

from pathlib import Path


def story_dir(workspace: str | Path) -> Path:
    """Top-level ``.story/`` inside a project workspace."""
    return Path(workspace) / ".story"


# ---- done ----


def done_dir(workspace: str | Path) -> Path:
    return story_dir(workspace) / "done"


def stage_done_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    return done_dir(workspace) / story_key / f"{stage}.json"


def stage_done_file_rel(story_key: str, stage: str) -> str:
    """Workspace-relative path of ``stage_done_file`` (single source of truth string).

    planner 把这个相对路径嵌进 CLI prompt(done 写位置)+ 自己在
    ``Path(workspace)/stage_done_file_rel`` 轮询;graph.py 用绝对 ``stage_done_file``
    检查。两者必须同布局 —— 本函数保证字符串与 ``stage_done_file`` 一致(写读对齐)。
    用原始 story_key(与读侧 graph.py 一致,不做 safe_segment)。
    """
    return f".story/done/{story_key}/{stage}.json"


# ---- context ----


def context_dir(workspace: str | Path, story_key: str) -> Path:
    return story_dir(workspace) / "context" / story_key


def plan_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    return context_dir(workspace, story_key) / f"plan_{stage}.md"


def review_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    return context_dir(workspace, story_key) / f"review_{stage}.md"


def done_snapshot_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    """Consumed done snapshot — written before source deletion."""
    return context_dir(workspace, story_key) / "done" / f"{stage}.json"


def malformed_done_file(workspace: str | Path, story_key: str, stage: str) -> Path:
    """Destination for un-parseable done files."""
    return context_dir(workspace, story_key) / "done" / f"{stage}.malformed"


def gate_report_dir(workspace: str | Path, story_key: str) -> Path:
    return context_dir(workspace, story_key) / "gates"


# ---- runs (benchmark) ----


def runs_dir(workspace_root: str | Path) -> Path:
    return story_dir(workspace_root) / "runs"


def swebench_run_dir(workspace_root: str | Path, run_id: str) -> Path:
    return runs_dir(workspace_root) / "swebench" / run_id


# ---- consult ----


def consult_dir(workspace: str | Path) -> Path:
    """advisory 结果目录(与 stage done 隔离,无 stage 推进语义)。

    consult 的产出是 advisory(建议),绝不能落进 ``.story/done/`` —— 那是
    stage 完成的握手文件目录,会被 graph / planner / orphan-claim 多处扫描,
    误落会被当成 stage 已完成(详见 DESIGN-consult-tool §3.5)。
    """
    return story_dir(workspace) / "consult"


def consult_result_file(workspace: str | Path, request_id: str) -> Path:
    """绝对路径:consult 单次请求的 advisory 结果文件。

    ``request_id`` 是 uuid hex[:12](同 clarify_server 的生成法),**不用**
    story_key / stage —— 同一 stage 可多次 consult,用 request_id 天然唯一。
    """
    return consult_dir(workspace) / f"{request_id}.json"


def consult_result_file_rel(request_id: str) -> str:
    """Workspace-relative path(嵌进 CLI prompt + 自身轮询)。

    与 ``consult_result_file`` 同布局(写读对齐,同 ``stage_done_file_rel``
    的不变式约定)。
    """
    return f".story/consult/{request_id}.json"
