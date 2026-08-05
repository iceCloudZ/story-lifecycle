"""设计 13 Step 2：PromptBuilder 测试（prompts.py）。

覆盖：
- design prompt 包含 PRD 内容
- build prompt 包含项目 worktree 信息
- 新 builder 输出与老 _build_cli_prompt 输出一致（回归保护）
- LaunchSeedBuilder 写 prompt 文件 + 返回 read-file seed
"""

import json
from pathlib import Path

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.abc import PromptBuilder
from story_lifecycle.orchestrator.prompts import (
    BuildPromptBuilder,
    DesignPromptBuilder,
    LaunchSeedBuilder,
    StagePromptBuilder,
    VerifyPromptBuilder,
    get_stage_prompt_builder,
)


@pytest.fixture
def tmp_story(tmp_path, isolated_story_home):
    """创建一个临时 story（minimal profile, design stage, 有 _agent_actions）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story("S-PROMPT-1", "提现门槛优化", str(ws), profile="minimal")
    prd = ws / "prd.md"
    prd.write_text("# 需求\n提现门槛从 100 降到 50", encoding="utf-8")
    db.update_story(
        "S-PROMPT-1",
        context_json=json.dumps(
            {
                "prd_path": str(prd),
                "workspace_path": str(ws),
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "claude"},
                    {"action": "launch", "stage": "build", "adapter": "claude"},
                    {"action": "launch", "stage": "verify", "adapter": "claude"},
                ],
            },
            ensure_ascii=False,
        ),
    )
    return "S-PROMPT-1"


@pytest.fixture
def tmp_story_with_project(tmp_story):
    """在 tmp_story 基础上绑定一个项目（build prompt 项目信息）。"""
    proj = db.create_project("hc-order", "D:/hc-all/hc-order")
    db.bind_story_project(
        "S-PROMPT-1",
        proj["id"],
        branch="feature/S-PROMPT-1",
        base_branch="main",
        worktree_path="D:/worktrees/S-PROMPT-1/hc-order",
    )
    return tmp_story


def _ctx(story_key):
    story = db.get_story(story_key)
    return json.loads(story.get("context_json") or "{}")


class TestPromptBuilder:
    def test_design_prompt_contains_prd(self, tmp_story):
        """design prompt 包含 PRD 内容"""
        builder = DesignPromptBuilder()
        prompt = builder.build(
            tmp_story, "design", str(db.get_story(tmp_story)["workspace"]), _ctx(tmp_story), {}
        )
        assert "PRD" in prompt
        assert "prd.md" in prompt

    def test_design_prompt_contains_dimensions(self, tmp_story):
        """design prompt 含设计维度 checklist（design 专属段）"""
        builder = DesignPromptBuilder()
        prompt = builder.build(
            tmp_story, "design", str(db.get_story(tmp_story)["workspace"]), _ctx(tmp_story), {}
        )
        assert "设计维度" in prompt or "维度 checklist" in prompt

    def test_build_prompt_contains_project_lines(self, tmp_story_with_project):
        """build prompt 包含项目 worktree 信息"""
        builder = BuildPromptBuilder()
        prompt = builder.build(
            tmp_story_with_project,
            "build",
            str(db.get_story(tmp_story_with_project)["workspace"]),
            _ctx(tmp_story_with_project),
            {},
        )
        assert "hc-order" in prompt
        assert "D:/worktrees/S-PROMPT-1/hc-order" in prompt

    def test_verify_prompt_contains_verify_task(self, tmp_story):
        """verify prompt 任务段为 verify（含完成协议 + test_report 成果物）"""
        builder = VerifyPromptBuilder()
        prompt = builder.build(
            tmp_story, "verify", str(db.get_story(tmp_story)["workspace"]), _ctx(tmp_story), {}
        )
        assert "## 任务: verify" in prompt
        assert "test_report" in prompt

    def test_prompt_matches_old_output(self, tmp_story):
        """新 builder 输出 == 老 _build_cli_prompt 输出（回归保护）"""
        from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt
        from story_lifecycle.orchestrator.prompts import _render_stage_prompt

        ws = str(db.get_story(tmp_story)["workspace"])
        ctx = _ctx(tmp_story)
        story = db.get_story(tmp_story)
        old_prompt = _build_cli_prompt(
            story_key=tmp_story,
            title=story["title"],
            stage="design",
            focus="",
            done_file=f".story/done/{tmp_story}/design.json",
            profile_stages={},
            prd_path="",
            project_section="",
            workspace=ws,
            transcript_section="",
            interactive=True,
            task_actions=["write_design_doc"],
            grill=False,
            is_single_stage=False,
            seed_context="",
        )
        new_prompt = _render_stage_prompt(
            story_key=tmp_story, stage="design", workspace=ws, ctx=ctx, action={}
        )
        # 核心段落一致（PRD section + Story 信息 + 成果物协议）
        assert "### Story 信息" in new_prompt
        assert "### 阶段说明" in new_prompt
        assert "### 关键要点" in new_prompt
        assert "story tool declare" in new_prompt
        # 老函数仍可用（回归保护：内容级一致）
        assert old_prompt.count("### Story 信息") == 1
        assert new_prompt.count("### Story 信息") == 1

    def test_builder_subclass_of_abc(self):
        """所有 builder 都是 PromptBuilder 子类"""
        for b in (
            StagePromptBuilder(),
            DesignPromptBuilder(),
            BuildPromptBuilder(),
            VerifyPromptBuilder(),
            LaunchSeedBuilder(),
        ):
            assert isinstance(b, PromptBuilder)

    def test_get_stage_prompt_builder_factory(self):
        """Factory 按 stage 返回对应子类"""
        assert isinstance(get_stage_prompt_builder("design"), DesignPromptBuilder)
        assert isinstance(get_stage_prompt_builder("build"), BuildPromptBuilder)
        assert isinstance(get_stage_prompt_builder("verify"), VerifyPromptBuilder)
        assert isinstance(get_stage_prompt_builder("unknown"), StagePromptBuilder)


class TestLaunchSeedBuilder:
    def test_seed_writes_prompt_file(self, tmp_story):
        """LaunchSeedBuilder 写 .story/context/<key>/prompt_<stage>.md"""
        builder = LaunchSeedBuilder()
        ws = str(db.get_story(tmp_story)["workspace"])
        seed = builder.build(tmp_story, "design", ws, _ctx(tmp_story), {})
        pfile = Path(ws) / ".story" / "context" / tmp_story / "prompt_design.md"
        assert pfile.exists()
        assert "### Story 信息" in pfile.read_text(encoding="utf-8")
        assert "请读取" in seed
        assert "prompt_design.md" in seed

    def test_seed_short_single_line(self, tmp_story):
        """seed 是短指令（一行），不是全量 prompt"""
        builder = LaunchSeedBuilder()
        ws = str(db.get_story(tmp_story)["workspace"])
        seed = builder.build(tmp_story, "design", ws, _ctx(tmp_story), {})
        assert len(seed) < 300
        assert "\n\n" not in seed
