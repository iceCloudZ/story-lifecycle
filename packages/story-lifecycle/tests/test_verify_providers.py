"""设计 10 改动 1:verify provider 扩展点 + external verify 证据接线测试。

覆盖:
- load_verify_provider:未配置 → None / duck-type 加载 / 失败容错(R6)
- R8 接线:_agent_actions 合入 done_data
- (迭代 3 G5:R2/R3 的 gate 本体合并逻辑随 unified_gate 删除;生产链路由
  stage_completion._run_external_verify_evidence 承担——证据进 judge 上下文,
  FAIL 落 test_failure finding,见 test_judge_three_decisions.TestExternalVerifyEvidence)

注:load_verify_provider 的扩展点测试全部保留(verify_providers 模块不动)。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.stage_completion import (
    _run_external_verify,
)
from story_lifecycle.orchestrator.verify_providers import load_verify_provider


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story(
        "EXT-VERIFY", "t", str(tmp_path), profile="minimal", current_stage="verify"
    )


# ---- load_verify_provider ----


def test_load_provider_none_when_unconfigured():
    assert load_verify_provider({}) is None
    assert load_verify_provider({"verify_provider": None}) is None


def test_load_provider_duck_type_no_abc(tmp_path, monkeypatch):
    """R6:只要求有 verify() 方法,不强制继承 BaseVerifyProvider(hc 侧无需装包)。"""
    mod = tmp_path / "hc_provider.py"
    mod.write_text(
        "class HcPytestVerifyProvider:\n"
        "    def __init__(self, config): self.config = config\n"
        "    def verify(self, story_key, workspace, stage, done_data):\n"
        "        return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    provider = load_verify_provider(
        {"verify_provider": {"module": "hc_provider", "class": "HcPytestVerifyProvider"}}
    )
    assert provider is not None
    assert provider.verify("k", "w", "verify", {}) is None


def test_load_provider_missing_verify_method_returns_none(tmp_path, monkeypatch):
    mod = tmp_path / "bad_provider.py"
    mod.write_text("class BadProvider:\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert (
        load_verify_provider(
            {"verify_provider": {"module": "bad_provider", "class": "BadProvider"}}
        )
        is None
    )


def test_load_provider_import_error_returns_none():
    """加载失败不阻断,降级到 LLM-only gate。"""
    assert (
        load_verify_provider(
            {"verify_provider": {"module": "no.such.module", "class": "X"}}
        )
        is None
    )


# ---- R8 接线（直接测 _run_external_verify，迭代 3 G5 迁移后） ----


def test_r8_wires_agent_actions_into_done_data():
    """R8:_run_external_verify 把 ctx["_agent_actions"] 合入 done_data,
    provider 读得到 selected_scenarios。"""
    spy = MagicMock()
    spy.verify.return_value = None
    actions = [
        {
            "action": "launch",
            "stage": "verify",
            "selected_scenarios": ["scenario:borrow-flow"],
        }
    ]
    with patch(
        "story_lifecycle.infra.config.get_config",
        return_value={"verify_provider": {"module": "x", "class": "Y"}},
    ):
        with patch(
            "story_lifecycle.orchestrator.verify_providers.load_verify_provider",
            return_value=spy,
        ):
            _run_external_verify(
                story_key="EXT-VERIFY",
                workspace="/tmp",
                done_data={"summary": "ok"},
                context={"_agent_actions": actions},
            )
    assert spy.verify.called
    passed_done = spy.verify.call_args.args[3]
    assert passed_done["_agent_actions"] == actions
    assert passed_done["_agent_actions"][0]["selected_scenarios"] == [
        "scenario:borrow-flow"
    ]
