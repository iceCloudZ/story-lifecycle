"""Tests for paths stage_done_file_rel (0d-B) —— done 写/读路径对齐。

planner 把 done_file_rel(workspace 相对)嵌进 CLI prompt 让它写,自己也在
``Path(workspace)/done_file_rel`` 轮询;graph.py 用 ``stage_done_file(workspace,...)``
检查。三者必须指向同一文件 —— 否则 done 永远收不到(全自动流水线断点 B)。

单一真相:``stage_done_file_rel`` 的字符串布局必须与 ``stage_done_file`` 一致。
"""

from pathlib import Path

from story_lifecycle.infra.paths import stage_done_file, stage_done_file_rel


def test_rel_layout_matches_done_subdir():
    assert stage_done_file_rel("FEAT-1", "implement") == ".story/done/FEAT-1/implement.json"


def test_workspace_concat_equals_stage_done_file():
    """Path(workspace)/rel == stage_done_file(workspace,...) —— 写读对齐不变式。"""
    ws = "D:/proj"
    rel = stage_done_file_rel("FEAT-9", "verify")
    assert Path(ws) / rel == stage_done_file(ws, "FEAT-9", "verify")


def test_rel_uses_raw_story_key_matching_absolute():
    """绝对/相对都用原始 story_key(与 graph.py 读侧一致,不 safe_segment)。"""
    rel = stage_done_file_rel("SUB-7", "release")
    assert "SUB-7" in rel
    assert "SUB_7" not in rel  # 不做 segment 转换
