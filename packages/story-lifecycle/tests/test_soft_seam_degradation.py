"""T4.3 · SOFT 缝降级测试。

架构不变量 #4:lifecycle 通过 try/except 软连接 miner/knowledge 等可选包,
这些包不可 import 时 lifecycle 必须继续运行,不能阻塞 prompt 渲染。

本测试用 monkeypatch 让 sys.modules 在 import 时抛 ImportError,
验证 `get_transcript_context` 和 `get_knowledge_context` 都返回 None 或降级内容,
而不是把异常抛给调用方。
"""

import sys

import pytest

from story_lifecycle.knowledge.context_providers import (
    get_knowledge_context,
    get_transcript_context,
)


class TestTranscriptContextSoftSeam:
    """miner 不可 import 时 get_transcript_context 降级返回 None。"""

    def test_returns_none_when_miner_unavailable(self, monkeypatch):
        """sys.modules['miner.config'] = None 让 import miner.config 抛 ImportError。"""
        monkeypatch.setitem(sys.modules, "miner.config", None)

        result = get_transcript_context("S-1", ".", "design")
        assert result is None

    def test_returns_none_when_configured_provider_unavailable(self, monkeypatch):
        """即使显式配置了 context_provider,import 失败也返回 None。"""
        from story_lifecycle.infra import config

        monkeypatch.setitem(sys.modules, "miner.story_context_provider", None)
        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "context_provider": {
                    "module": "miner.story_context_provider",
                    "class": "TranscriptStoryContextProvider",
                }
            },
        )

        result = get_transcript_context("S-1", ".", "design")
        assert result is None


class TestKnowledgeContextSoftSeam:
    """knowledge 包不可 import 时 get_knowledge_context 不崩。"""

    @pytest.fixture(autouse=True)
    def _empty_kroot(self, tmp_path, monkeypatch):
        """钉死知识根到空 tmp:降级注入(B1③)后无 task_type 也会读根,
        不钉住会碰到真实全局知识库(D:/hc-all)。"""
        import story_lifecycle.infra.config as _cfg_mod

        monkeypatch.setattr(_cfg_mod, "get_config", lambda: {})
        monkeypatch.setenv("STORY_KNOWLEDGE_ROOT", str(tmp_path / "empty-kroot"))

    def test_returns_something_without_knowledge_package(self, isolated_story_home, monkeypatch):
        """knowledge 包缺失时,knowledge_provider 跳过知识库段落,其余内容仍返回。"""
        monkeypatch.setitem(sys.modules, "knowledge", None)

        # seed a story with task_type so the provider has something to render
        from story_lifecycle.infra.db import models as db

        db.upsert_story(
            "S-KNOW",
            title="knowledge seam test",
            workspace=str(isolated_story_home),
            profile="minimal",
            status="active",
            context_json='{"task_type": "fund-flow"}',
        )

        result = get_knowledge_context("S-KNOW", str(isolated_story_home), "design")
        # Should not crash; may be None if no artifacts, or a string without knowledge index section.
        assert result is None or "知识库" not in result

    def test_unknown_task_type_degrades_to_none_when_no_global_knowledge(
        self, isolated_story_home, monkeypatch
    ):
        """B1③ 新契约:无 task_type 走降级注入(全局层);降级层也无内容 → None。

        旧契约(无 task_type 直接 None)已于飞轮闭环 M1 废除 —— 它让 87% story
        零注入。knowledge import 被阻断 + 空知识根 → 降级层无内容 → None,
        soft-seam 语义(不崩、不阻塞)保持不变。
        """
        monkeypatch.setitem(sys.modules, "knowledge", None)

        from story_lifecycle.infra.db import models as db

        db.upsert_story(
            "S-NO-TYPE",
            title="no task type",
            workspace=str(isolated_story_home),
            profile="minimal",
            status="active",
            context_json="{}",
        )

        result = get_knowledge_context("S-NO-TYPE", str(isolated_story_home), "design")
        assert result is None
