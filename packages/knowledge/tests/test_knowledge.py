"""Tests for the unified knowledge layer."""
import json
import os

import pytest

from knowledge import KnowledgeIndex
from knowledge.generator import generate_index, write_index
from knowledge.models import FileRef, PlaybookEntry, ScenarioEntry


def _write(knowledge_dir: str, rel_path: str, content: str) -> str:
    path = os.path.join(knowledge_dir, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def test_generate_index_collects_scenarios_and_playbooks(tmp_path):
    _write(
        str(tmp_path),
        "scenarios/order/create.md",
        "# 下单场景\n\n## 概述\n创建订单流程。\n",
    )
    _write(
        str(tmp_path),
        "playbooks/debug.md",
        "# 排查/Debug Playbook\n\n## 必看文件\n- `OrderController.java`\n",
    )
    _write(
        str(tmp_path),
        "playbooks/debug.md.json",
        json.dumps(
            {
                "theme": "debug",
                "session_count": 5,
                "top_files": [
                    {"path": "order/OrderController.java", "role": "Controller", "count": 3}
                ],
            },
            ensure_ascii=False,
        ),
    )

    index = generate_index(str(tmp_path))
    assert index["version"] == 1
    assert len(index["entries"]) == 2

    ids = {e["id"] for e in index["entries"]}
    assert "scenario:create" in ids
    assert "playbook:debug" in ids

    pb = next(e for e in index["entries"] if e["type"] == "playbook")
    assert pb["theme"] == "debug"
    assert pb["top_files"][0]["path"] == "order/OrderController.java"


def test_knowledge_index_retrieves_by_domain_and_query(tmp_path):
    _write(
        str(tmp_path),
        "scenarios/order/create.md",
        "# 下单场景\n\n## 概述\n创建订单。\n",
    )
    _write(
        str(tmp_path),
        "scenarios/payment/pay.md",
        "# 支付场景\n\n## 概述\n发起支付。\n",
    )
    _write(
        str(tmp_path),
        "playbooks/debug.md",
        "# 排查/Debug\n\n## 必看文件\n- `OrderController.java`\n",
    )
    _write(
        str(tmp_path),
        "playbooks/debug.md.json",
        json.dumps({"theme": "debug", "session_count": 5}, ensure_ascii=False),
    )

    index = KnowledgeIndex(str(tmp_path))
    results = index.retrieve(domain="order", query="创建订单", top_k=5)
    assert len(results) >= 1
    assert any(e.title == "下单场景" for e in results)


def test_knowledge_index_by_story_playbook_priority(tmp_path):
    _write(
        str(tmp_path),
        "playbooks/by-story/STORY-42.md",
        "# Story STORY-42 Playbook\n\n> 复盘\n",
    )
    _write(
        str(tmp_path),
        "playbooks/by-story/STORY-42.md.json",
        json.dumps(
            {"linked_story": "STORY-42", "session_count": 3},
            ensure_ascii=False,
        ),
    )

    index = KnowledgeIndex(str(tmp_path))
    results = index.retrieve(story_key="STORY-42", top_k=5)
    assert results[0].type == "playbook"
    assert results[0].linked_story == "STORY-42"


def test_failure_knowledge_in_index(tmp_path):
    failures = {
        "failures": [
            {
                "id": "failure:compile",
                "type": "failure",
                "title": "编译错误",
                "source": "dynamic",
                "category": "compile_error",
                "display_category": "编译错误",
                "detail": "cannot find symbol",
            }
        ]
    }
    _write(
        str(tmp_path),
        "failures/failure-knowledge.json",
        json.dumps(failures, ensure_ascii=False),
    )

    index = generate_index(str(tmp_path))
    assert len(index["entries"]) == 1
    assert index["entries"][0]["type"] == "failure"


def test_write_index_creates_file(tmp_path):
    _write(
        str(tmp_path),
        "scenarios/order/create.md",
        "# 下单场景\n",
    )
    path = write_index(str(tmp_path))
    assert os.path.exists(path)
    assert os.path.basename(path) == "INDEX.json"


def test_scenario_parser_extracts_legacy_markdown_fields(tmp_path):
    _write(
        str(tmp_path),
        "scenarios/order/create.md",
        "# Create Order\n\n"
        "## Participants\n"
        "- hc-order: order creation\n"
        "- hc-user: user validation\n\n"
        "## Flow\n"
        "1. User submits order\n"
        "2. System validates stock\n\n"
        "## Data Tables\n"
        "- t_order\n"
        "- t_order_item\n\n"
        "## MQ Messages\n"
        "- Produces `ORDER_CREATED`\n",
    )
    index = generate_index(str(tmp_path))
    scenario = next(e for e in index["entries"] if e["type"] == "scenario")
    assert set(scenario["participating_services"]) == {"hc-order", "hc-user"}
    assert scenario["main_flow"] == ["User submits order", "System validates stock"]
    assert scenario["tables"] == ["t_order", "t_order_item"]
    assert scenario["mq_topics"] == ["ORDER_CREATED"]


def test_attribution_reports_merged_into_failure_knowledge(tmp_path):
    report = {
        "instance_id": "tapd-1065518",
        "root_cause_category": "timeout",
        "root_cause_detail": "CI step timed out",
        "failure_stage": "verify",
        "counterfactual_candidates": ["increase CI timeout"],
    }
    _write(
        str(tmp_path),
        "failures/attribution-reports/tapd-1065518.json",
        json.dumps(report, ensure_ascii=False),
    )

    from knowledge.generator import merge_attribution_reports

    path = merge_attribution_reports(str(tmp_path))
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    ids = {f["id"] for f in payload["failures"]}
    assert "failure:attribution:tapd-1065518" in ids
