"""T2.5 · PTY/HITL bug 回归(最近 5 个 fix(pty)).

最近 PTY 注入试错链(522d26a8 → 5e65535c + 3fefbd65)最终被 `claude "query"` 模式取代。
本测试回归当前正确行为:
- `interactive_launch_cmd(model, prompt="X")` 返回 `["claude", "X"]`
- `interactive_launch_cmd(model, prompt="")` 返回 `["claude"]`(空白 claude,无 arg)

不测试已废弃的 bracketed-paste / readiness-marker / PTY 注入逻辑。
"""

from __future__ import annotations

import pytest

from story_lifecycle.infra.terminal.platform_ops import resolve_executable
from story_lifecycle.knowledge.adapters.claude import ClaudeAdapter


@pytest.fixture
def adapter() -> ClaudeAdapter:
    return ClaudeAdapter()


def test_interactive_launch_with_prompt_uses_query_mode(adapter):
    """Regression: prompt must be passed as `claude "query"` arg, not via PTY injection."""
    prompt = "请读取 .story/context/STORY-1/prompt_design.md 并执行"
    argv = adapter.interactive_launch_cmd(model="claude-3-5-sonnet", prompt=prompt)

    claude_exe = resolve_executable("claude")
    assert argv == [claude_exe, prompt]


def test_interactive_launch_without_prompt_starts_blank_claude(adapter):
    """Regression: empty prompt must start a blank `claude` session, no extra arg."""
    argv = adapter.interactive_launch_cmd(model="claude-3-5-sonnet", prompt="")

    claude_exe = resolve_executable("claude")
    assert argv == [claude_exe]


def test_interactive_launch_with_session_id_and_prompt(adapter):
    """`claude --session-id ID --name NAME prompt` form is preserved."""
    prompt = "继续任务"
    argv = adapter.interactive_launch_cmd(
        model="claude-3-5-sonnet",
        prompt=prompt,
        session_id="sess-123",
        session_name="STORY-1-design",
    )

    claude_exe = resolve_executable("claude")
    assert argv == [
        claude_exe,
        "--session-id",
        "sess-123",
        "--name",
        "STORY-1-design",
        prompt,
    ]


def test_interactive_launch_resume_with_prompt(adapter):
    """Resume form: `claude --resume ID prompt`."""
    prompt = "继续"
    argv = adapter.interactive_launch_cmd(
        model="claude-3-5-sonnet",
        prompt=prompt,
        session_id="sess-123",
        resume=True,
    )

    claude_exe = resolve_executable("claude")
    assert argv == [claude_exe, "--resume", "sess-123", prompt]
