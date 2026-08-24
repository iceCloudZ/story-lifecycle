"""ensure_task_type 单测 —— 所有创建路径的唯一 task_type 打标入口。

回归背景(飞轮断点 B1):task_type 此前只在 create_and_start_story 打标,
bugs/sync 等 sourced 创建路径(upsert_story_from_source)整批漏标,
实测覆盖率 4/30 → 87% story 飞轮知识注入为零。
"""

import json

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.service import story_service as svc


def _mk_story(key: str, title: str = "") -> None:
    db.create_story(key, title, "D:/ws", current_stage="design")


def _ctx_task_type(key: str):
    story = db.get_story(key)
    ctx = json.loads(story.get("context_json") or "{}")
    return ctx.get("task_type")


class TestEnsureTaskType:
    def test_keyword_hit_persists(self):
        """关键词命中 → 写入 context_json.task_type。"""
        _mk_story("T-1", "还款流程报错修复")
        tt = svc.ensure_task_type("T-1", use_llm=False)
        assert tt in ("fund-flow", "debug")
        assert _ctx_task_type("T-1") == tt

    def test_idempotent_existing_not_overwritten(self, monkeypatch):
        """已有 task_type → 直接返回,不调 LLM/关键词,不覆盖。"""
        _mk_story("T-2", "还款")
        db.update_context("T-2", "task_type", "fund-flow")
        monkeypatch.setattr(
            svc,
            "_classify_task_type_llm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调 LLM")),
        )
        assert svc.ensure_task_type("T-2") == "fund-flow"
        assert _ctx_task_type("T-2") == "fund-flow"

    def test_llm_preferred_over_keywords(self, monkeypatch):
        """use_llm=True 时 LLM 结果优先(BUG #16:关键词首命中会误分)。"""
        _mk_story("T-3", "Loan Disclosure 展示")
        monkeypatch.setattr(svc, "_classify_task_type_llm", lambda *a: "frontend")
        assert svc.ensure_task_type("T-3") == "frontend"
        assert _ctx_task_type("T-3") == "frontend"

    def test_llm_failure_falls_back_to_keyword(self, monkeypatch):
        """LLM 失败(返回 None)→ 关键词兜底;都不中 → None,不阻塞。"""
        _mk_story("T-4", "授信额度调整")
        monkeypatch.setattr(svc, "_classify_task_type_llm", lambda *a: None)
        assert svc.ensure_task_type("T-4") == "credit-limit"

        _mk_story("T-5", "xyz 无匹配标题")
        assert svc.ensure_task_type("T-5") is None
        assert _ctx_task_type("T-5") is None

    def test_batch_path_keyword_only(self, monkeypatch):
        """批量 sync 路径 use_llm=False → 绝不调 LLM(不为每 item 阻塞几秒)。"""
        _mk_story("T-6", "订单状态异常")
        monkeypatch.setattr(
            svc,
            "_classify_task_type_llm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调 LLM")),
        )
        assert svc.ensure_task_type("T-6", use_llm=False) in ("order", "debug")

    def test_title_falls_back_to_db(self):
        """调用方不传 title → 从 DB 读(sourced 重同步场景)。"""
        _mk_story("T-7", "营销券活动")
        assert svc.ensure_task_type("T-7", use_llm=False) == "marketing"
