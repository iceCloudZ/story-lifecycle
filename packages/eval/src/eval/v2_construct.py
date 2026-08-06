"""快照 v2 §2.2 决策分支构造样本 — 伪造/注入逼出未触发分支。

三条（任务 §2.2）:
1. 伪造悬而未决 HIGH finding ×2 → 期望 fail/escalate（gate decision=retry/fail）
2. 注入 playbook「换 adapter 成功」记录 ×2 → 期望 swap_approach
3. 模拟缺依赖 done_data ×2 → 期望 insert_rescue_stage

构造方式: 改 done_data / DB finding 种子 / workspace playbook 文件,
**不改被测代码**。本脚本产出 ``construct_samples.json``（构造清单）+
可复现的种子函数（round 3 验收时调用）。

分支触发机制（实测自 unified_gate.py / findings.py / reflection.py）:
- HIGH → fail: ``db.findings.create_finding(severity="high")`` 落 open finding →
  ``get_open_findings(story_key, min_severity="high")`` 非空 → evidence.open_high_findings
  → LLM 判 retry/fail（HIGH 未解决）
- swap_approach: ``_load_playbook_for_verify(workspace, task_type)`` 读
  ``<workspace>/.story/knowledge/playbooks/<task_type>/<dimension>.md``,
  含「adapter X 失败 → 换 Y 成功」规则 → prompt 历史经验 → LLM 判 swap_approach
- insert_rescue_stage: done_data 缺 files_changed/test_report + finding 缺依赖类
  → prompt「缺依赖等」→ LLM 判 insert_rescue_stage
"""
from __future__ import annotations

import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"


def build_construct_samples() -> list[dict]:
    """6 条构造样本清单（seed 数据按 spec 生成，round 3 执行 gate 时调用 seed 函数）。"""
    return [
        {
            "category": "construct",
            "cls": "c1-high-finding",
            "name": "伪造悬而未决 HIGH finding（conformance）",
            "tapd_id": "CONSTRUCT-HIGH-1",
            "story_key": "construct-high-1",
            "seed": {
                "finding": {
                    "story_key": "construct-high-1", "stage": "verify", "source": "conformance",
                    "severity": "high", "category": "conformance",
                    "description": "conformance: alignment=2 coverage=2 scope_drift=2 | 需求核心功能未实现（构造样本）",
                    "location": "conformance_check",
                },
                "done_data": {"summary": "verify 完成（构造）", "files_changed": ["src/main/java/App.java"]},
                "adapter": "opencode", "task_type": "JAVA",
            },
            "expected": "gate verdict=rework/fail, decision=retry/fail（HIGH finding 未解决,不可 advance）",
            "reason": "逼出 gate fail 分支（round 2.5 缺口 1: gate decision=fail 从未触发）",
            "verify_script": "v2_construct.seed_high_finding(cfg)",
        },
        {
            "category": "construct",
            "cls": "c2-high-finding",
            "name": "伪造悬而未决 HIGH finding（test）",
            "tapd_id": "CONSTRUCT-HIGH-2",
            "story_key": "construct-high-2",
            "seed": {
                "finding": {
                    "story_key": "construct-high-2", "stage": "verify", "source": "unified_gate",
                    "severity": "high", "category": "test",
                    "description": "test-report.md 缺失,验收用例未执行（构造样本）",
                    "location": "test_report",
                },
                "done_data": {"summary": "verify 完成（构造）", "files_changed": ["src/test/T.java"]},
                "adapter": "opencode", "task_type": "JAVA",
            },
            "expected": "gate verdict=rework/fail（HIGH test 类 finding 阻断）",
            "reason": "逼出 gate fail 分支（HIGH finding 类别变体: test）",
            "verify_script": "v2_construct.seed_high_finding(cfg)",
        },
        {
            "category": "construct",
            "cls": "c3-swap-approach",
            "name": "注入 playbook 换 adapter 成功（opencode→kimi）",
            "tapd_id": "CONSTRUCT-SWAP-1",
            "story_key": "construct-swap-1",
            "seed": {
                "playbook": {
                    "task_type": "JAVA", "dimension": "recovery",
                    "rules": ["adapter opencode 失败 → 换 kimi 成功"],
                },
                "done_data": {"summary": "verify 完成（构造）", "files_changed": ["src/main/java/App.java"],
                              "findings_hint": [{"severity": "medium", "category": "quality",
                                                 "description": "实现与 spec 部分偏差（构造）"}]},
                "adapter": "opencode", "task_type": "JAVA",
            },
            "expected": "gate verdict=rework, repair_action.kind=swap_approach（历史经验显示换 adapter 有效）",
            "reason": "逼出 swap_approach 分支（round 2.5 缺口 1: repair_action=swap_approach 从未触发）",
            "verify_script": "v2_construct.seed_playbook(cfg)",
        },
        {
            "category": "construct",
            "cls": "c4-swap-approach",
            "name": "注入 playbook 换 adapter 成功（codex→claude）",
            "tapd_id": "CONSTRUCT-SWAP-2",
            "story_key": "construct-swap-2",
            "seed": {
                "playbook": {
                    "task_type": "JAVA", "dimension": "recovery",
                    "rules": ["adapter codex 失败 → 换 claude 成功"],
                },
                "done_data": {"summary": "verify 完成（构造）", "files_changed": ["src/main/java/App.java"],
                              "findings_hint": [{"severity": "medium", "category": "quality",
                                                 "description": "多次 retry 未收敛（构造）"}]},
                "adapter": "codex", "task_type": "JAVA",
            },
            "expected": "gate verdict=rework, repair_action.kind=swap_approach, new_adapter=claude",
            "reason": "swap_approach 变体（不同 adapter 组合）",
            "verify_script": "v2_construct.seed_playbook(cfg)",
        },
        {
            "category": "construct",
            "cls": "c5-rescue-stage",
            "name": "模拟缺依赖 done_data（无 files_changed/test_report）",
            "tapd_id": "CONSTRUCT-RESCUE-1",
            "story_key": "construct-rescue-1",
            "seed": {
                "done_data": {"summary": "verify 完成（构造）", "files_changed": []},
                "adapter": "opencode", "task_type": "JAVA",
            },
            "expected": "gate verdict=rework, repair_action.kind=insert_rescue_stage（缺依赖/证据不全）",
            "reason": "逼出 insert_rescue_stage 分支（round 2.5 缺口 1: repair_action=insert_rescue_stage 从未触发）",
            "verify_script": "v2_construct.seed_done_data(cfg)",
        },
        {
            "category": "construct",
            "cls": "c6-rescue-stage",
            "name": "模拟缺依赖（workspace 无 spec/PRD 且 done 空）",
            "tapd_id": "CONSTRUCT-RESCUE-2",
            "story_key": "construct-rescue-2",
            "seed": {
                "done_data": {"summary": "", "files_changed": [], "findings_hint": []},
                "adapter": "opencode", "task_type": "JAVA",
                "workspace_empty": True,
            },
            "expected": "gate verdict=rework, repair_action.kind=insert_rescue_stage 或 escalate",
            "reason": "insert_rescue_stage 变体（极端缺依赖）",
            "verify_script": "v2_construct.seed_done_data(cfg)",
        },
    ]


def seed_high_finding(cfg: dict) -> None:
    """种 open HIGH finding 到 STORY_HOME DB（不改被测代码）。"""
    import os

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.infra.db import findings

    db.init_db()
    f = cfg["seed"]["finding"]
    fid = findings.create_finding(
        story_key=f["story_key"], stage=f["stage"], source=f["source"],
        severity=f["severity"], category=f["category"],
        description=f["description"], location=f.get("location"),
    )
    print(f"seeded finding {fid} for {f['story_key']} (severity={f['severity']})")


def seed_playbook(cfg: dict) -> None:
    """写 workspace playbook 文件（_load_playbook_for_verify 读的路径）。"""
    import os

    p = cfg["seed"]["playbook"]
    ws = Path(os.environ.get("STORY_WORKSPACE", "construct-ws"))
    d = ws / ".story" / "knowledge" / "playbooks" / p["task_type"]
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{p['dimension']}.md"
    body = "\n".join(f"- {r}" for r in p["rules"]) + "\n"
    f.write_text(body, encoding="utf-8")
    print(f"seeded playbook {f}")


def seed_done_data(cfg: dict) -> None:
    """构造 done_data（gate 调用的输入，由验收脚本读取）。"""
    print(f"done_data seed spec: {json.dumps(cfg['seed']['done_data'], ensure_ascii=False)[:120]}")


def main() -> dict:
    SNAP_V2.mkdir(parents=True, exist_ok=True)
    samples = build_construct_samples()
    (SNAP_V2 / "construct_samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"construct_samples": len(samples), "path": str(SNAP_V2 / "construct_samples.json")}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
