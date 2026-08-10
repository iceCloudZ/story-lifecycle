"""构造种子：sandbox-ui/story_home 新建 DB，种 construct 6 条 + v2 代表样本。

场景（用户重点：HIGH finding / swap / 缺依赖 三类 UI 展示）：
- construct-high-1/2: HIGH finding（conformance/test）→ UI 应展示 open HIGH finding
- construct-swap-1/2: playbook 换 adapter → 详情页可见历史经验
- construct-rescue-1/2: 缺依赖（空 done + 无 spec）→ 缺依赖 UI 展示
- v2 样本: 3 条真实 story（A/B/C 各一）带 gate_result 历史 → gate 结果展示素材

用 sqlite 直接种（不改核心包）。workspace 放 sandbox-ui/ws/<key>/。
is_test=0（list_visible_stories 默认隐藏 is_test=1）。
"""
import json
import sqlite3
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(r"D:\github\story-lifecycle\packages\eval\sandbox-ui\story_home")
WS = Path(r"D:\github\story-lifecycle\packages\eval\sandbox-ui\ws")
DB = HOME / "story.db"
HOME.mkdir(parents=True, exist_ok=True)

SRC_DB = r"D:\github\story-lifecycle\packages\eval\sandbox\story_home\story.db"
if not DB.exists():
    shutil.copy2(SRC_DB, DB)
    con = sqlite3.connect(DB)
    for t in ("story", "finding", "gate_result", "stage_log", "event_log", "llm_trace", "orchestrator_decision"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
    con.close()
    print("schema copied + cleared")

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add_story(key, title, status="paused", stage="verify", lifecycle="待启动", source_id=""):
    con = sqlite3.connect(DB)
    ws = WS / key
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "AGENTS.md").write_text("# sandbox-ui replay workspace\n\n仅 UI 验证用。\n", encoding="utf-8")
    st = "tapd" if source_id else None
    con.execute(
        "INSERT INTO story (story_key, title, workspace, profile, current_stage, status, context_json, execution_count, created_at, updated_at, source_type, source_id, lifecycle_state, is_test, intake_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, title, str(ws), "replay-nb", stage, status, "{}", 0, now, now, st, source_id or "", lifecycle, 0, "ready"),
    )
    sid = con.execute("SELECT id FROM story WHERE story_key=?", (key,)).fetchone()[0]
    con.commit()
    con.close()
    return sid


def add_finding(story_key, severity, category, description, stage="verify", source="conformance"):
    con = sqlite3.connect(DB)
    fid = f"finding-{uuid.uuid4().hex[:12]}"
    con.execute(
        "INSERT INTO finding (id, story_key, stage, source, severity, category, location, description, status, evidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, story_key, stage, source, severity, category, "conformance_check", description, "open", "[]", now, now),
    )
    con.commit()
    con.close()
    return fid


def add_gate(story_id, stage, gate_name, result, detail):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO gate_result (story_id, stage, gate_name, result, detail, created_at) VALUES (?,?,?,?,?,?)",
        (story_id, stage, gate_name, result, detail, now),
    )
    con.commit()
    con.close()


def add_doc(story_key, doc_type, content):
    d = WS / story_key / "story"
    d.mkdir(parents=True, exist_ok=True)
    fname = {"prd": "PRD.md", "spec": "spec.md"}[doc_type]
    (d / fname).write_text(content, encoding="utf-8")


print("== HIGH finding ==")
sid = add_story("construct-high-1", "[构造] HIGH finding: conformance 未实现", stage="verify", lifecycle="开发")
add_finding("construct-high-1", "high", "conformance",
            "conformance: alignment=2 coverage=2 scope_drift=2 | 需求核心功能未实现（构造样本）：反欺诈 1*N 证件优化仅交付枚举文本调整，未实现证件去重与多场景规则。")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "verify 存在未解决 HIGH finding（conformance）",
    "findings": [{"severity": "high", "category": "conformance", "description": "需求核心功能未实现（构造样本）"}],
}, ensure_ascii=False))
add_doc("construct-high-1", "prd", "# 反欺诈 1*N 证件优化\n\n- 证件去重\n- 多场景规则\n- 证件照片展示")

sid = add_story("construct-high-2", "[构造] HIGH finding: 测试缺失", stage="verify", lifecycle="开发")
add_finding("construct-high-2", "high", "test",
            "test-report.md 缺失,验收用例未执行（构造样本）: smoke/integration 测试全部未跑。")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "HIGH test 类 finding 阻断",
    "findings": [{"severity": "high", "category": "test", "description": "验收用例未执行（构造样本）"}],
}, ensure_ascii=False))
add_doc("construct-high-2", "prd", "# 还款路由版本控制\n\n- 支持配置最高 app 版本\n- 边界判断\n- 前端校验")

print("== swap_approach ==")
sid = add_story("construct-swap-1", "[构造] playbook: 换 adapter 成功(opencode→kimi)", stage="verify", lifecycle="开发")
add_finding("construct-swap-1", "medium", "quality",
            "实现与 spec 部分偏差（构造样本）: 多次 retry 未收敛。")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "历史经验显示换 adapter 有效",
    "repair_action": {"kind": "swap_approach", "reason": "playbook 显示 adapter opencode 失败 → 换 kimi 成功", "new_adapter": "kimi"},
}, ensure_ascii=False))
add_doc("construct-swap-1", "prd", "# OTP 短信路由\n\n- 运营商路由配置\n- 测试用户白名单")
pb = WS / "construct-swap-1" / ".story" / "knowledge" / "playbooks" / "JAVA"
pb.mkdir(parents=True, exist_ok=True)
(pb / "recovery.md").write_text("- adapter opencode 失败 → 换 kimi 成功\n", encoding="utf-8")

sid = add_story("construct-swap-2", "[构造] playbook: 换 adapter 成功(codex→claude)", stage="verify", lifecycle="开发")
add_finding("construct-swap-2", "medium", "quality",
            "多次 retry 未收敛（构造样本）: 实现与 spec 部分偏差。")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "playbook 显示换 adapter 有效",
    "repair_action": {"kind": "swap_approach", "reason": "adapter codex 失败 → 换 claude 成功", "new_adapter": "claude"},
}, ensure_ascii=False))
add_doc("construct-swap-2", "prd", "# 提现门槛规则更新\n\n- 组合额度判断\n- 券核销")

print("== rescue_stage ==")
sid = add_story("construct-rescue-1", "[构造] 缺依赖: 无 files_changed/test_report", stage="verify", lifecycle="开发")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "缺依赖/证据不全,建议插入救援 stage",
    "repair_action": {"kind": "insert_rescue_stage", "reason": "done_data 无 files_changed/test_report", "rescue_stage": "verify"},
}, ensure_ascii=False))
add_doc("construct-rescue-1", "prd", "# 新营销中台上送\n\n- 业务节点事件\n- 事件中心")

sid = add_story("construct-rescue-2", "[构造] 极端缺依赖: 空 done + 无文档", stage="verify", lifecycle="开发")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "产出完全为空,需要救援",
    "repair_action": {"kind": "insert_rescue_stage", "reason": "无任何产出物", "rescue_stage": "build"},
}, ensure_ascii=False))

print("== v2 samples ==")
sid = add_story("tapd-1144381896001067103", "REPLAY 1144381896001067103 (A 类, gate pass)", status="completed", stage="verify", lifecycle="测试", source_id="1144381896001067103")
add_gate(sid, "verify", "unified_gate", "pass", json.dumps({
    "verdict": "pass", "decision": "advance", "reason": "无未解决 HIGH finding,产出符合要求",
    "findings": [],
}, ensure_ascii=False))
add_doc("tapd-1144381896001067103", "prd", "# 还款路由支持配置最高app版本\n\n- 支持最高版本配置\n- 版本区间判断\n- 前端校验")

sid = add_story("tapd-1144381896001028664", "REPLAY 1144381896001028664 (B 类, gate retry)", status="completed", stage="verify", lifecycle="测试", source_id="1144381896001028664")
add_gate(sid, "verify", "unified_gate", "rework", json.dumps({
    "verdict": "rework", "decision": "retry", "reason": "verify 产出摘要较简略;变更文件与需求相关度需确认",
    "findings": [{"severity": "medium", "category": "quality", "description": "实现与 spec 部分偏差:ID Similarity 未实现实时 1:N 查询"},
                 {"severity": "low", "category": "process", "description": "verify 产出摘要较简略,未列出具体验证用例"}],
}, ensure_ascii=False))
add_doc("tapd-1144381896001028664", "prd", "# 反欺诈 1*N 证件优化\n\n- 证件去重\n- 多场景规则")

sid = add_story("tapd-1144381896001065519", "REPLAY 1144381896001065519 (C 类, story_refs)", status="completed", stage="verify", lifecycle="测试", source_id="1144381896001065519")
add_gate(sid, "verify", "unified_gate", "pass", json.dumps({
    "verdict": "pass", "decision": "advance", "reason": "OTP 短信路由验证通过",
    "findings": [],
}, ensure_ascii=False))
add_doc("tapd-1144381896001065519", "prd", "# OTP短信路由\n\n- 运营商路由配置\n- 白名单")

print("seeded. DB:", DB)
