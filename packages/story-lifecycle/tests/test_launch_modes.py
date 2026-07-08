"""T2.4 · 三启动模式一致性(-p / query / release).

验证三种 claude 启动方式对同一 story/prompt 产出可验证的等价启动意图:
- headless: `claude -p ...`(prompt 经 stdin 注入)
- interactive: `claude "<prompt>"`
- release: 纯文本 release checklist
"""

from __future__ import annotations

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.knowledge.adapters.claude import ClaudeAdapter
from story_lifecycle.orchestrator.context.release_prompt import generate_release_prompt


@pytest.fixture
def prompt_text() -> str:
    return "实现用户登录接口并补充单元测试"


@pytest.fixture
def story(tmp_path):
    return db.create_story(
        story_key="STORY-LM-1",
        title="用户登录功能",
        workspace=str(tmp_path),
        profile="minimal",
        current_stage="verify",
    )


def test_headless_launch_uses_p_flag(prompt_text):
    """Headless mode uses `claude -p` with tool/permission flags; prompt goes via stdin."""
    adapter = ClaudeAdapter()
    argv = adapter.headless_launch_cmd(model="claude-3-5-sonnet", prompt=prompt_text)

    assert argv is not None
    assert "claude" in argv[0]
    assert "-p" in argv
    assert "--allowedTools" in argv
    assert "--permission-mode" in argv


def test_interactive_launch_includes_query(prompt_text):
    """Interactive mode uses `claude "<prompt>"` query form."""
    adapter = ClaudeAdapter()
    argv = adapter.interactive_launch_cmd(
        model="claude-3-5-sonnet",
        prompt=prompt_text,
    )

    assert argv is not None
    assert "claude" in argv[0]
    assert prompt_text in argv


def test_release_prompt_is_plain_text_containing_story(story):
    """Release mode returns plain-text prompt containing story context."""
    result = generate_release_prompt(story["story_key"])

    assert "content" in result
    assert result["story_key"] == story["story_key"]
    content = result["content"]
    assert "# 上线前准备" in content
    assert story["title"] in content
    assert story["story_key"] in content


def test_three_modes_share_same_executable_and_story_intent(prompt_text, story):
    """Cross-mode baseline: all three modes reference the same adapter and story intent."""
    adapter = ClaudeAdapter()

    headless_argv = adapter.headless_launch_cmd(model="", prompt=prompt_text)
    interactive_argv = adapter.interactive_launch_cmd(model="", prompt=prompt_text)
    release = generate_release_prompt(story["story_key"])

    # All modes use the claude executable
    assert "claude" in headless_argv[0]
    assert "claude" in interactive_argv[0]

    # Interactive and release directly carry prompt/story text;
    # headless carries `-p` flag indicating stdin prompt feed.
    assert "-p" in headless_argv
    assert prompt_text in interactive_argv
    assert story["title"] in release["content"]
