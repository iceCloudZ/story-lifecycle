"""Tests for consult path helpers(§5.4 / 实施步骤 1)。

与 ``stage_done_file_rel`` 隔离是不变式:consult 的 advisory 落进 ``.story/consult/``,
绝不复用 ``.story/done/``(那是 stage 完成握手目录,被 graph / planner / orphan-claim
多处扫描,误落会被当成 stage 已完成;见 DESIGN-consult-tool §3.5)。
"""

from pathlib import Path

from story_lifecycle.infra.paths import (
    consult_dir,
    consult_result_file,
    consult_result_file_rel,
    stage_done_file_rel,
)


def test_consult_dir_under_story_not_done():
    """consult 目录在 .story/consult/,不在 .story/done/。"""
    ws = "D:/proj"
    assert consult_dir(ws) == Path(ws) / ".story" / "consult"
    # 显式隔离不变式
    assert "done" not in str(consult_dir(ws))


def test_consult_result_file_rel_layout():
    """rel 路径格式 .story/consult/<rid>.json。"""
    rid = "abc123def456"
    assert consult_result_file_rel(rid) == f".story/consult/{rid}.json"


def test_workspace_concat_equals_consult_result_file():
    """Path(workspace)/rel == consult_result_file —— 写读对齐不变式。

    (同 ``stage_done_file_rel`` 的对齐约定;runner 用 rel 嵌 prompt,
    planner/CLI 用绝对路径读,两者必须同文件。)
    """
    ws = "D:/proj"
    rid = "req789abc012"
    rel = consult_result_file_rel(rid)
    assert Path(ws) / rel == consult_result_file(ws, rid)


def test_consult_isolated_from_done_namespace():
    """consult 结果文件路径绝不与 stage_done_file_rel 撞名空间。"""
    rid = "FEAT-1"
    rel = consult_result_file_rel(rid)
    done_rel = stage_done_file_rel(rid, "implement")
    assert not rel.startswith(".story/done/")
    assert done_rel.startswith(".story/done/")
    # 即使 request_id 恰好等于 story_key,两者也不撞(不同父目录)
    assert rel != done_rel
