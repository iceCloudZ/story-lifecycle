"""T3.3 approval_queue 阻塞部署 -- 现状澄清测试。

任务卡要求验证 deploy stage 在无 approval 时阻塞。调查发现:
1. stage_library.py 的 deploy 确实定义了 requires_human=True。
2. 但所有内置 profile(minimal/strict/demo/headless-smoke/realtest/swebench)的 stages
   列表里都没有 deploy;实际执行流程由 profile 决定,因此 deploy 默认不会被调度。
3. /api/approvals 端点存在,但它返回的是 pending findings(质量飞轮的待处理发现),
   不是部署审批队列。
4. 当前代码没有一处检查 StageDefinition.requires_human 来阻塞 stage 执行。

因此本卡的目标转为 记录现状并锁定契约:
- deploy 定义保留 requires_human=True(未来启用时的语义标记)。
- 默认 profile 不启用 deploy(避免误触发生产部署)。
- /api/approvals 返回 findings(现有行为不变)。

本测试不模拟不存在的阻塞逻辑,而是把这些现状断言下来,防止未来有人把 deploy
偷偷加进默认 profile 或把 requires_human 误删。
"""

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.orchestrator.engine.profile_loader import resolve_profile
from story_lifecycle.orchestrator.engine.stage_library import get_stage_definition
from story_lifecycle.orchestrator.service.api import app
from story_lifecycle.infra.db import models as db


BUILTIN_PROFILES = [
    "minimal",
    "strict",
    "demo",
    "headless-smoke",
    "realtest",
    "swebench",
]


@pytest.fixture
def client(isolated_story_home):
    """FastAPI TestClient with isolated DB."""
    return TestClient(app)


class TestDeployStageDefinition:
    """deploy stage 在 stage_library 中的定义契约。"""

    def test_deploy_requires_human(self):
        """deploy 阶段必须标记为需要人参与。"""
        stage_def = get_stage_definition("deploy")
        assert stage_def is not None
        assert stage_def.requires_human is True
        assert stage_def.category.value == "deployment"
        assert stage_def.risk.value == "critical"

    def test_other_human_stages_also_require_human(self):
        """human_review / architecture_review 同样 requires_human(参照组)。"""
        for name in ("human_review", "architecture_review"):
            assert get_stage_definition(name).requires_human is True


class TestDeployNotInDefaultProfiles:
    """默认 profile 不启用 deploy,这是当前 不阻塞也安全 的根因。"""

    @pytest.mark.parametrize("profile_name", BUILTIN_PROFILES)
    def test_deploy_not_in_profile_stages(self, profile_name):
        """所有内置 profile 的 stages 里都没有 deploy。"""
        profile = resolve_profile(profile_name)
        assert "deploy" not in profile.stages

    @pytest.mark.parametrize("profile_name", BUILTIN_PROFILES)
    def test_deploy_not_in_next_default(self, profile_name):
        """next_default 也不指向 deploy,防止隐式进入。"""
        profile = resolve_profile(profile_name)
        for cfg in profile.stages.values():
            assert "deploy" not in cfg.next_default


class TestApprovalsEndpointReturnsFindings:
    """/api/approvals 当前实现返回的是 pending findings,不是部署审批。"""

    def test_approvals_endpoint_returns_findings_list(self, client, isolated_story_home):
        """空库时返回空 findings 列表,端点可访问。"""
        r = client.get("/api/approvals")
        assert r.status_code == 200
        data = r.json()
        assert "findings" in data
        assert data["findings"] == []

    def test_approvals_includes_pending_finding(self, client, isolated_story_home):
        """当有一个 open finding 时,/api/approvals 会返回它。"""
        db.upsert_story("S-DEPLOY", title="t", workspace=str(isolated_story_home), profile="minimal", status="active")
        db.create_finding(
            story_key="S-DEPLOY",
            stage="verify",
            source="rule",
            severity="HIGH",
            category="test",
            description="missing test",
        )

        r = client.get("/api/approvals")
        assert r.status_code == 200
        data = r.json()
        assert len(data["findings"]) >= 1
        finding = data["findings"][0]
        assert finding["story_key"] == "S-DEPLOY"
        assert finding["severity"] == "HIGH"
