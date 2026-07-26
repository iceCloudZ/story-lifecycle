"""/api/approvals 端点现状澄清测试。

任务卡要求验证 deploy stage 在无 approval 时阻塞。调查发现:
1. 所有内置 profile(minimal/strict/demo/headless-smoke/realtest/swebench)的 stages
   列表里都没有 deploy;实际执行流程由 profile 决定,因此 deploy 默认不会被调度。
2. /api/approvals 端点存在,但它返回的是 pending findings(质量飞轮的待处理发现),
   不是部署审批队列。

(原 stage_library.py 的 deploy StageDefinition 契约断言已随 phase6 死代码团删除 ——
 stage_library 整模块零生产引用,其 requires_human 标记无人消费。)

本测试不模拟不存在的阻塞逻辑,而是把 /api/approvals 的现状断言下来,防止未来
有人改端点语义。
"""

import pytest
from fastapi.testclient import TestClient

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


class TestDeployNotInDefaultProfiles:
    """默认 profile 不启用 deploy。"""

    @pytest.mark.parametrize("profile_name", BUILTIN_PROFILES)
    def test_deploy_not_in_profile_stages(self, profile_name):
        """所有内置 profile 的 stages 里都没有 deploy。"""
        from story_lifecycle.orchestrator.engine.profile_loader import resolve_profile

        profile = resolve_profile(profile_name)
        assert "deploy" not in profile.stages

    @pytest.mark.parametrize("profile_name", BUILTIN_PROFILES)
    def test_deploy_not_in_next_default(self, profile_name):
        """next_default 也不指向 deploy,防止隐式进入。"""
        from story_lifecycle.orchestrator.engine.profile_loader import resolve_profile

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
