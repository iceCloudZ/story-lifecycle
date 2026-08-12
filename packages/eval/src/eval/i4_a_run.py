# -*- coding: utf-8 -*-
"""迭代4 A 线：三格对照实验（模拟证据 + 现场 conformance + judge_stage_completion）。

- 证据模拟层：session 行 / events.jsonl / test_report.md（全样本同一套中性模板）
- A1 中性审计：34 条归一化 diff（仅样本固有字段可差异）+ 质量词黑名单扫描
- 逐样本落盘：v2 标签分 / 现场 conformance 分 / 判决 / reason 全文
- 预注册决策规则在报告阶段应用（脚本只出数字）
"""
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
SL = r"D:\github\story-lifecycle"
sys.path.insert(0, SL + r"\packages\eval\src")
sys.path.insert(0, SL + r"\packages\story-lifecycle\src")
from eval import v2_rebase  # noqa: E402

v2_rebase.hook_llm_calls()

BASE = Path(SL) / "packages/eval/dataset"
SNAP2 = BASE / "snapshot_v2_20260806"
OUT_DIR = Path(SL) / "packages/eval/results"
OUT_JSONL = OUT_DIR / "i4_abc_20260812.jsonl"
AUDIT = OUT_DIR / "i4_neutral_audit_20260812.json"

# ---------------- pre-flight ----------------
from eval.judges import configure_llm_env  # noqa: E402

configure_llm_env()
base_url = os.environ.get("STORY_LLM_BASE_URL", "")
model = os.environ.get("STORY_LLM_MODEL", "")
print(f"[pre-flight] STORY_LLM_BASE_URL={base_url} model={model}", flush=True)
assert "zen/go/v1" in base_url, f"端点偏离 Go: {base_url}"

grid = json.loads((BASE / "i4_abc_grid_20260812.json").read_text(encoding="utf-8"))
samples_all = [json.loads(l) for l in SNAP2.joinpath("replay_samples.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
held = [json.loads(l) for l in SNAP2.joinpath("held_out.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
rows = [json.loads(l) for l in SNAP2.joinpath("merge_scores.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
idx = {(r["repo"], r["merge_hash"]): r for r in rows}
dels = {d["repo"] + ":" + d["merge_hash"]: d for d in
        [json.loads(l) for l in BASE.joinpath("deliveries.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]}

# 样本元信息（tapd_id/evidence 从 held/replay_samples 匹配）
held_meta = {h["merge"]: h for h in held}
samp_meta = {s["merge"]: s for s in samples_all if s.get("merge")}

# ---------------- 证据模拟层（中性模板） ----------------
QUALITY_WORDS = ["完美", "优秀", "糟糕", "缺陷", "不足", "低", "高", "充分", "不完整", "完整",
                 "质量", "问题", "失败原因", "严重", "偏差", "缺失", "未实现", "未覆盖"]

SIM_FILE_NAMES = [
    "src/main/java/App.java", "src/main/java/Service.java", "src/main/java/Repository.java",
    "src/main/java/Controller.java", "src/main/java/Config.java", "src/test/AppTest.java",
    "src/test/ServiceTest.java", "src/test/IntegrationTest.java",
    "src/test/ControllerTest.java", "src/test/RepositoryTest.java",
    "src/main/resources/application.yml", "src/main/java/Dto.java",
    "src/main/java/Entity.java", "src/main/java/Mapper.java", "src/main/java/Handler.java",
    "src/test/E2eTest.java", "src/main/java/Task.java", "src/main/java/Event.java",
    "src/main/java/Util.java", "src/main/java/Factory.java",
]


def sim_files(n: int) -> list[str]:
    """按 diffstat 文件数量生成中性文件清单（全样本同规则）。"""
    n = max(0, min(int(n or 0), len(SIM_FILE_NAMES)))
    return SIM_FILE_NAMES[:n]

EVENTS_TEMPLATE = [
    {"ts": "2026-08-01T00:00:00.000Z", "dir": ".", "text": "Running: git status"},
    {"ts": "2026-08-01T00:00:01.000Z", "dir": ".", "text": "Reading: src/main/java/App.java"},
    {"ts": "2026-08-01T00:00:02.000Z", "dir": ".", "text": "Executing: python -m pytest -q"},
    {"ts": "2026-08-01T00:00:03.000Z", "dir": ".", "text": "Collecting test items"},
    {"ts": "2026-08-01T00:00:04.000Z", "dir": ".", "text": "File written: src/main/java/App.java"},
    {"ts": "2026-08-01T00:00:05.000Z", "dir": ".", "text": "Running: git diff --stat"},
    {"ts": "2026-08-01T00:00:06.000Z", "dir": ".", "text": "Executing: python -m pytest tests/test_a.py"},
    {"ts": "2026-08-01T00:00:07.000Z", "dir": ".", "text": "File written: src/test/T.java"},
    {"ts": "2026-08-01T00:00:08.000Z", "dir": ".", "text": "Running: python -m pytest tests/test_b.py"},
    {"ts": "2026-08-01T00:00:09.000Z", "dir": ".", "text": "Collecting test items"},
    {"ts": "2026-08-01T00:00:10.000Z", "dir": ".", "text": "Executing: python -m pytest --tb=short"},
    {"ts": "2026-08-01T00:00:11.000Z", "dir": ".", "text": "Reading: story/test_report.md"},
    {"ts": "2026-08-01T00:00:12.000Z", "dir": ".", "text": "File written: story/test_report.md"},
    {"ts": "2026-08-01T00:00:13.000Z", "dir": ".", "text": "Running: git log --oneline -3"},
    {"ts": "2026-08-01T00:00:14.000Z", "dir": ".", "text": "Executing: python -m pytest tests/test_c.py"},
    {"ts": "2026-08-01T00:00:15.000Z", "dir": ".", "text": "Collecting test items"},
    {"ts": "2026-08-01T00:00:16.000Z", "dir": ".", "text": "File written: story/spec.md"},
    {"ts": "2026-08-01T00:00:17.000Z", "dir": ".", "text": "Running: git status --porcelain"},
    {"ts": "2026-08-01T00:00:18.000Z", "dir": ".", "text": "Executing: python -m pytest tests/test_d.py"},
    {"ts": "2026-08-01T00:00:19.000Z", "dir": ".", "text": "File written: story/delivery.md"},
]


def test_report_text(files: list[str]) -> str:
    n = len(files or [])
    fl = "\n".join(f"- {f}" for f in (files or [])[:20])
    # 执行记录条数 = N（与「N passed」一致——避免计数矛盾被 judge 识别）；
    # 全样本同规则（条数随 N 派生）
    runs = "\n".join(f"- [PASS] case_{i:03d}: \u6267\u884c\u6821\u9a8c\u7528\u4f8b\u901a\u8fc7" for i in range(1, max(n, 1) + 1))
    return (f"# \u6d4b\u8bd5\u62a5\u544a\n\n## \u6267\u884c\u7ed3\u679c\n{n} passed / 0 failed\n\n"
            f"## \u6267\u884c\u8bb0\u5f55\n{runs}\n\n## \u8986\u76d6\u6587\u4ef6\n{fl}\n")


def inject_simulated_evidence(ws: Path, story_key: str, files: list[str], db_module) -> None:
    """session 行 + events.jsonl + test_report.md（中性模板，全样本一致规则）。"""
    # 1. session 行
    try:
        db_module.upsert_session(story_key, "verify", "opencode", session_id="sim-session", status="completed")
        with db_module._db() as conn:
            conn.execute(
                "UPDATE story_session SET attempt=1, outcome='success', failure_reason='', "
                "artifacts_prod=?, pty_log_ref=? WHERE story_key=? AND stage='verify' AND adapter='opencode'",
                (json.dumps(files[:20], ensure_ascii=False), str(ws / ".story/runs" / story_key / "pty_verify"),
                 story_key),
            )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("i4").warning("session 注入失败: %s", exc)
    # 2. events.jsonl
    runs = ws / ".story" / "runs" / story_key / "pty_verify"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in EVENTS_TEMPLATE) + "\n", encoding="utf-8")
    # 3. test_report.md（verify 成果物，统一连字符文件名——覆盖 evidence 复制的旧文件，
    #    保证 judge 只读到中性模板；N 与用例条数从 files_changed 派生，全样本同规则）
    story_dir = ws / "story"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "test-report.md").write_text(test_report_text(files), encoding="utf-8")
    (story_dir / "test_report.md").write_text(test_report_text(files), encoding="utf-8")  # 双名兼容


# ---------------- 中性审计 ----------------
def audit_neutrality(items: list[dict]) -> dict:
    """对每个样本生成归一化模拟证据快照 → 全局 diff + 词表扫描。"""
    snapshots = {}
    for it in items:
        dl = dels.get(it["repo"] + ":" + it["merge"]) or {}
        ds = dl.get("diffstat") or {}
        files = sim_files(ds.get("files") if isinstance(ds, dict) else 0)
        tr = test_report_text(files)
        import re as _re

        tr_norm = _re.sub(r"case_\d{3}", "case_NNN", tr)
        tr_norm = _re.sub(r"\d+ passed", "N passed", tr_norm)
        # 执行记录行随 N 派生（固有数字）——移除后比较固定部分；
        # 覆盖文件清单 = 样本固有字段（允许差异）——同样剥离
        tr_fixed = _re.sub(r"- \[PASS\] case_NNN:.*\n?", "", tr_norm)
        tr_fixed = tr_fixed.split("## 覆盖文件")[0]
        ev = "\n".join(json.dumps(e, ensure_ascii=False) for e in EVENTS_TEMPLATE)
        snapshots[it["merge"][:10]] = {"test_report_fixed": tr_fixed, "events": ev,
                                       "session_attempt": "1", "session_outcome": "success",
                                       "n_files": len(files)}
    norms = [s["test_report_fixed"] + "\n" + s["events"] for s in snapshots.values()]
    uniform = len(set(norms)) == 1
    bad_words = {}
    for k, s in snapshots.items():
        hit = [w for w in QUALITY_WORDS if w in s["test_report_fixed"] or w in s["events"]]
        if hit:
            bad_words[k] = hit
    return {"uniform": uniform, "n_samples": len(snapshots),
            "quality_word_hits": bad_words,
            "sample_n_files": {k: s["n_files"] for k, s in snapshots.items()}}


# ---------------- runner ----------------
def run_one(it: dict, tmp_home: Path, grid_name: str, db_module) -> dict:
    repo, mh = it["repo"], it["merge"]
    story_key = f"i4-{grid_name}-{mh[:8]}"
    ws = tmp_home / story_key
    ws.mkdir(parents=True, exist_ok=True)
    os.environ["STORY_HOME"] = str(ws)
    os.environ["STORY_WORKSPACE"] = str(ws)
    db_module.init_db()
    db_module.create_story(story_key, title=story_key, workspace=str(ws), profile="minimal", current_stage="verify")
    from story_lifecycle.orchestrator.evaluation.stage_completion import (
        JudgeRequest,
        judge_stage_completion,
    )

    row = idx.get((repo, mh))
    dl = dels.get(repo + ":" + mh) or {}
    ds = dl.get("diffstat") or {}
    files = sim_files(ds.get("files") if isinstance(ds, dict) else 0)
    t0 = time.monotonic()
    spec_path = ""
    prd_text = ""
    artifacts: list[str] = []
    if row and (row.get("tapd_id") or ""):
        idxm, tapd = v2_rebase.load_match_index()
        linked = idxm.get((repo, mh))
        if linked:
            ent = linked["entity"]
            ref_text, ref_type = v2_rebase.reference_for(ent, tapd)
            prd_text = ref_text
            if ref_text:
                refs = ws / "refs"
                refs.mkdir(parents=True, exist_ok=True)
                spec_path = str(refs / "ref.md")
                Path(spec_path).write_text(ref_text, encoding="utf-8")
            from eval.evidence_snapshot import evidence_dir_for

            ev_dir = evidence_dir_for(ent.get("evidence_dir") or "")
            if ev_dir and ev_dir.is_dir():
                ws_story = ws / "story"
                ws_story.mkdir(parents=True, exist_ok=True)
                for f in ev_dir.iterdir():
                    if f.is_file() and f.suffix.lower() == ".md":
                        (ws_story / f.name).write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                for cand in ("story/spec.md", "story/test-report.md", "story/PRD.md"):
                    if (ws_story / Path(cand).name).exists():
                        artifacts.append(cand)
    if prd_text:
        db_module.upsert_story_doc(story_key, "prd", prd_text[:100_000], change_reason="replay", author="eval")
    # diff 落临时文件（conformance 通道）
    from eval import scanall

    diff_text, _ = scanall._diff_text(repo, mh)
    diff_path = ""
    if diff_text:
        diff_path = str(ws / "delivery.diff")
        Path(diff_path).write_text(diff_text, encoding="utf-8")
    ms = (row or {}).get("merge_summary") or {}
    summary = (ms.get("summary") or "").strip()
    if not summary:
        commits = dl.get("commits") or []
        summary = "；".join((c.get("message") or c.get("subject") or "")[:80] for c in commits[:10])
    done_data = {"summary": summary[:500], "files_changed": files, "spec_path": spec_path,
                 "delivery_diff_path": diff_path}
    # 模拟证据注入
    inject_simulated_evidence(ws, story_key, files, db_module)
    # 模拟注入的 test-report.md 声明进成果物（judge 必须读到——格 1 放行的证据基础）
    if (ws / "story" / "test-report.md").exists():
        artifacts.append("story/test-report.md")
    req = JudgeRequest(
        story_key=story_key, stage="verify", workspace=str(ws),
        ctx={"conformance_check": True, "task_type": ""}, lifecycle_state="验证",
        done_data=done_data, adapter="opencode", retry_count=1,
        artifacts=artifacts or None,
    )
    try:
        result = judge_stage_completion(req)
        q = result.get("quality")
        dm = {"approve": "advance", "reject": "retry", "escalate": "escalate"}
        rec = {
            "grid": grid_name, "repo": repo, "merge10": mh[:10],
            "v2_align": it.get("align"), "v2_cov": it.get("cov"),
            "gate_verdict": "pass" if q == "approve" else "rework",
            "gate_decision": dm.get(q, q or "ERR"),
            "gate_reason_full": result.get("reason") or "",
            "gate_repair": (result.get("repair_action") or {}),
            "fallback": bool(result.get("fallback")),
            "elapsed_s": round(time.monotonic() - t0, 1),
        }
    except Exception as e:  # noqa: BLE001
        rec = {"grid": grid_name, "repo": repo, "merge10": mh[:10], "error": str(e)[:300]}
    return rec


def main() -> None:
    from story_lifecycle.infra.db import models as db_module

    items = [dict(g, grid="g1") for g in grid["grid1"]] + [dict(g, grid="g2") for g in grid["grid2"]]
    if os.environ.get("I4_DRY"):
        pick = [items[0], items[1], items[len(grid["grid1"])]]  # 格1取2 + 格2取1
        items = pick
        print(f"[dry-run] {len(items)} 条（格1取2 + 格2取1）", flush=True)
    # A1 中性审计（全量样本，dry-run 也跑）
    audit = audit_neutrality(items)
    print(f"[A1] uniform={audit['uniform']} n={audit['n_samples']} 词表命中={audit['quality_word_hits']}", flush=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_home = Path(tempfile.mkdtemp(prefix="i4_abc_"))
    done = {}
    if OUT_JSONL.exists():
        for l in OUT_JSONL.read_text(encoding="utf-8").splitlines():
            if l.strip():
                r = json.loads(l)
                done[r["merge10"]] = r
    f = open(OUT_JSONL, "a", encoding="utf-8")
    for i, it in enumerate(items):
        if it["merge"][:10] in done:
            continue
        rec = run_one(it, tmp_home, it["grid"], db_module)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        print(f"[{i+1}/{len(items)}] {it['grid']} {it['repo']}:{it['merge'][:10]} -> "
              f"{rec.get('gate_decision') or rec.get('error', 'ERR')[:60]} ({rec.get('elapsed_s', 0)}s)", flush=True)
    f.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
