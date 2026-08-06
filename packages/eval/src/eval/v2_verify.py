"""快照 v2 §4 五项自验收脚本。

1. merge_scores.jsonl = v1 同键 750 行，error ≤2%，LLM 日志全程 Go（无 api.deepseek.com）
2. baseline 自洽性落盘；replay_samples ≥50 条（说明池约束）、21 主题全覆盖、v1 样本去重
3. held-out 15 条满足全部排除规则
4. git status 核心包（packages/story-lifecycle/src）零改动
5. 汇总输出（供回复）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_V1 = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"
RESULTS = PACKAGE_ROOT / "results"
SANDBOX = PACKAGE_ROOT / "sandbox"


def check1() -> dict:
    """merge_scores 行数 / error / 全程 Go。"""
    rows: dict[tuple[str, str], dict] = {}
    for line in (SNAP_V2 / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[(r["repo"], r["merge_hash"])] = r
    errs = [r for r in rows.values() if r.get("error")]
    calls = (RESULTS / "v2_rebase_calls.log").read_text(encoding="utf-8").splitlines()
    go = sum(1 for l in calls if "opencode.ai" in l)
    deep = sum(1 for l in calls if "api.deepseek.com" in l)
    ok = (len(rows) == 750 and len(errs) / max(len(rows), 1) <= 0.02 and deep == 0)
    return {
        "ok": bool(ok), "rows": len(rows), "errors": len(errs),
        "calls_go": go, "calls_deepseek": deep,
        "error_list": [f"{r['repo']}:{r['merge_hash'][:10]}: {r['error'][:60]}" for r in errs],
    }


def check2() -> dict:
    """baseline 自洽性 + samples 覆盖/去重。"""
    b = json.loads((SNAP_V2 / "baseline_v2.json").read_text(encoding="utf-8"))
    c = b.get("consistency") or {}
    samples = [json.loads(l) for l in (SNAP_V2 / "replay_samples.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    topics = {s["topic"] for s in samples if s.get("category") == "topic"}
    # v1 样本去重（§2.3 要求，仅 abcd 类别）：samples20 + b_inject 的 tapd_id 不在 abcd 样本里
    samples20 = json.loads((SANDBOX / "gold" / "samples20.json").read_text(encoding="utf-8"))
    b_inject = json.loads((RESULTS / "pipeline_b_injected_20260805.json").read_text(encoding="utf-8"))
    exclude_tapd = {s["tapd_id"] for s in samples20} | {s["tapd_id"] for s in b_inject}
    leaked = [s["tapd_id"] for s in samples if s.get("category", "").startswith("abcd") and s.get("tapd_id") in exclude_tapd]
    keys = [(s.get("repo"), (s.get("merge") or s.get("merge10") or "")[:10]) for s in samples]
    dups = [k for k, n in Counter(keys).items() if n > 1 and k != (None, "")]
    return {
        "ok": bool(len(topics) == 21 and not leaked and not dups),
        "samples_n": len(samples),
        "topics_covered": len(topics),
        "leaked_v1_tapd": leaked,
        "dup_keys": dups,
        "consistency": c.get("diff_le_1_ratio"),
        "consistency_pairs": c.get("n_pairs"),
        "baseline_errors": len(b.get("errors") or []),
    }


def check3() -> dict:
    """held-out 15 条排除规则校验。"""
    held = [json.loads(l) for l in (SNAP_V2 / "held_out.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    samples20 = json.loads((SANDBOX / "gold" / "samples20.json").read_text(encoding="utf-8"))
    b_inject = json.loads((RESULTS / "pipeline_b_injected_20260805.json").read_text(encoding="utf-8"))
    exclude_tapd = {s["tapd_id"] for s in samples20} | {s["tapd_id"] for s in b_inject}
    gate_keys = set()
    for f in (RESULTS / "gate_replay_refs_20260805").glob("*.md"):
        m = re.match(r"(pos|neg)_([^_]+)_([0-9a-f]{10,})_", f.name)
        if m:
            gate_keys.add((m.group(2), m.group(3)))
    sample_keys = set()
    for line in (SNAP_V2 / "replay_samples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            s = json.loads(line)
            sample_keys.add((s.get("repo", ""), (s.get("merge") or s.get("merge10") or "")[:10]))
    # 人工确认/再裁决
    human_keys: set[tuple[str, str]] = set()
    for line in (SNAP_V1 / "stories_matched.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            for dl in e.get("deliveries", []):
                if dl.get("human_confirmed") or dl.get("human_recalibrated") or e.get("human_recalibrated"):
                    human_keys.add((dl["repo"], dl["merge_hash"][:10]))
    violations = []
    for h in held:
        k = (h["repo"], h["merge"][:10])
        tid = h["tapd_id"]
        if tid in exclude_tapd:
            violations.append(f"{k} tapd in v1 samples")
        if k in gate_keys:
            violations.append(f"{k} in gate replay 167")
        if k in sample_keys:
            violations.append(f"{k} in replay_samples")
        if k in human_keys:
            violations.append(f"{k} human confirmed/recalibrated")
        if not h.get("sealed"):
            violations.append(f"{k} not sealed")
    return {"ok": bool(len(held) == 15 and not violations), "held_n": len(held), "violations": violations}


def check4() -> dict:
    """git status：核心包零改动。"""
    r = subprocess.run(
        ["git", "status", "--porcelain", "--", "packages/story-lifecycle"],
        cwd=PACKAGE_ROOT.parent, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    changed = [l for l in r.stdout.splitlines() if l.strip()]
    return {"ok": bool(not changed), "changed": changed}


def main() -> dict:
    results = {"check1_merge_scores": check1(), "check2_samples_baseline": check2(), "check3_held_out": check3(), "check4_core_untouched": check4()}
    all_ok = all(v["ok"] for v in results.values())
    results["all_ok"] = bool(all_ok)
    (RESULTS / "v2_acceptance_20260806.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    main()
