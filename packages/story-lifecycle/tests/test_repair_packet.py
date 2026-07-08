"""T1.4 · evaluator_loop repair-packet 构造.

对 build_repair_packet 做输入→输出契约测试,不依赖真实 LLM。
"""

from __future__ import annotations

import pytest

from story_lifecycle.orchestrator.evaluation.evaluator_loop import build_repair_packet


@pytest.fixture
def packet_inputs() -> dict:
    return {
        "story_key": "STORY-123",
        "stage": "verify",
        "plan_summary": "跑测试并产出验证报告",
        "stage_output_summary": "verify stage completed",
        "findings": [
            {
                "severity": "high",
                "category": "logic",
                "description": "登录接口未校验密码强度",
                "location": "auth.py:42",
                "recommendation": "增加密码复杂度校验",
            },
            {
                "severity": "medium",
                "category": "style",
                "description": "变量命名不清晰",
                "location": "user.py:10",
            },
        ],
        "verification": {"status": "blocked_by_gate"},
        "round_num": 2,
    }


def test_repair_packet_contains_stage_and_round(tmp_path, packet_inputs):
    """Packet must contain stage name and round number."""
    packet = build_repair_packet(
        workspace=str(tmp_path),
        write_file=False,
        **packet_inputs,
    )

    assert "verify" in packet
    assert "Round 2" in packet


def test_repair_packet_contains_each_finding_description(tmp_path, packet_inputs):
    """Packet must contain every finding description (substring match)."""
    packet = build_repair_packet(
        workspace=str(tmp_path),
        write_file=False,
        **packet_inputs,
    )

    for finding in packet_inputs["findings"]:
        assert finding["description"] in packet


def test_repair_packet_contains_plan_summary_and_verification(tmp_path, packet_inputs):
    """Packet must carry plan summary and verification status."""
    packet = build_repair_packet(
        workspace=str(tmp_path),
        write_file=False,
        **packet_inputs,
    )

    assert packet_inputs["plan_summary"] in packet
    assert packet_inputs["verification"]["status"] in packet


def test_repair_packet_empty_findings_does_not_crash(tmp_path, packet_inputs):
    """Edge case: empty findings list must not raise and should still produce a packet."""
    inputs = {**packet_inputs, "findings": []}
    packet = build_repair_packet(
        workspace=str(tmp_path),
        write_file=False,
        **inputs,
    )

    assert isinstance(packet, str)
    assert "Repair Packet" in packet


def test_repair_packet_write_to_disk(tmp_path, packet_inputs):
    """Optional: write_file=True writes the packet to the expected path."""
    result = build_repair_packet(
        workspace=str(tmp_path),
        write_file=True,
        **packet_inputs,
    )

    expected_path = (
        tmp_path / ".story" / "context" / packet_inputs["story_key"]
        / f"repair_{packet_inputs['stage']}_round{packet_inputs['round_num']}.md"
    )
    assert result == str(expected_path)
    assert expected_path.exists()
    content = expected_path.read_text(encoding="utf-8")
    assert packet_inputs["plan_summary"] in content
    assert packet_inputs["findings"][0]["description"] in content
