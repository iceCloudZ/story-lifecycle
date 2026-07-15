"""design-dimensions section: interactive vs autonomous clarify protocol.

Interactive terminal spawns claude via `claude "query"` WITHOUT --mcp-config,
so it has no `mcp__lifecycle__clarify` tool — the prompt must tell it to ask the
human directly in the terminal instead. The autonomous path (headless -p, with
MCP) keeps the MCP clarify instruction.
"""
from story_lifecycle.orchestrator.engine.prompt_sections import (
    build_design_dimensions_section,
)


def test_interactive_omits_mcp_clarify_and_asks_human_in_terminal(tmp_path):
    s = build_design_dimensions_section("IP-1", str(tmp_path), "design", interactive=True)
    assert "mcp__lifecycle__clarify" not in s  # interactive claude has no MCP tool
    assert "终端" in s  # tell claude to ask the watching human directly


def test_default_keeps_mcp_clarify_for_autonomous_path(tmp_path):
    s = build_design_dimensions_section("IP-1", str(tmp_path), "design")
    assert "mcp__lifecycle__clarify" in s  # headless -p has the MCP tool


def test_interactive_keeps_dimensions_checklist(tmp_path):
    s = build_design_dimensions_section("IP-1", str(tmp_path), "design", interactive=True)
    assert "设计维度 checklist" in s  # only the clarify protocol changes
    assert "brainstorming" in s  # the no-brainstorming rule stays


# ============================================================================
# REFACTOR §5.2.2 — build_design_dimensions_section 分层路由
# 全局维度 playbook(根目录)+ task_type 特定经验(子目录,reflect 产出)
# ============================================================================


def test_global_playbook_still_guided_when_no_task_type(tmp_path):
    """无 task_type 时只引导全局维度 playbook(向后兼容)。"""
    # 放一个全局 playbook
    playbooks_dir = tmp_path / ".story" / "knowledge" / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (playbooks_dir / "security-parameter-trust.md").write_text("# stub", encoding="utf-8")

    s = build_design_dimensions_section("NO-TASK", str(tmp_path), "design")
    assert "security-parameter-trust.md" in s
    assert "通用" in s


def test_task_type_specific_playbook_guided(tmp_path, monkeypatch):
    """task_type 子目录有 playbook → 引导"本任务类型历史经验"。"""
    playbooks_dir = tmp_path / ".story" / "knowledge" / "playbooks"
    task_dir = playbooks_dir / "credit-limit"
    task_dir.mkdir(parents=True)
    (task_dir / "adapter-routing.md").write_text("# stub experience", encoding="utf-8")

    # mock task_type 查询
    from story_lifecycle.orchestrator.engine import prompt_sections
    monkeypatch.setattr(prompt_sections, "_get_task_type", lambda _sk: "credit-limit")

    s = build_design_dimensions_section("S-1", str(tmp_path), "design")
    assert "adapter-routing.md" in s
    assert "模型路由经验" in s or "历史经验" in s


def test_no_playbook_files_no_crash(tmp_path):
    """playbooks 目录不存在时也不崩(failsafe)。"""
    s = build_design_dimensions_section("S-1", str(tmp_path), "design")
    assert "设计维度 checklist" in s  # checklist 骨架仍在
