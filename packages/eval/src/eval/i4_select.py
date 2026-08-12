# -*- coding: utf-8 -*-
"""迭代4 A 线：三格选样 + 证据模拟层 + A1 中性审计。

格 1（合格 15）：held-out 争议 4 + abcd-A 4 + topic 放行 2 + v2 合格池抽 5（align>=4 cov>=4，种子 42，排除已用）
格 2（应拦 19）：replay_samples topic 应拦 19
证据模拟：session 行 / events.jsonl / test_report.md——同一套中性模板（除样本固有字段外逐字节一致）
"""
import json
import random
from pathlib import Path

BASE = Path(r"D:\github\story-lifecycle\packages\eval\dataset")
SNAP2 = BASE / "snapshot_v2_20260806"

samples = [json.loads(l) for l in SNAP2.joinpath("replay_samples.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
held = [json.loads(l) for l in SNAP2.joinpath("held_out.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
rows = [json.loads(l) for l in SNAP2.joinpath("merge_scores.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

# ---- 格 1 ----
g1_controversial = [h for h in held if (h.get("v2_align") or 0) >= 4 and (h.get("v2_cov") or 0) >= 3]
assert len(g1_controversial) == 4, len(g1_controversial)
g1_abcd_a = [s for s in samples if s.get("category") == "abcd-A"]
assert len(g1_abcd_a) == 4
g1_topic_pass = [s for s in samples if s["category"] == "topic" and "放行" in s["expected"]]
assert len(g1_topic_pass) == 2
used_keys = {h["merge"] for h in g1_controversial} | {s["merge"] for s in g1_abcd_a} | {s["merge"] for s in g1_topic_pass}
pool = [r for r in rows if (r.get("conformance_score") or {}).get("alignment", 0) >= 4
        and (r.get("conformance_score") or {}).get("coverage", 0) >= 4
        and r.get("merge_hash") not in used_keys and r.get("tapd_id")]
rng = random.Random(42)
g1_pool5 = rng.sample(pool, 5)
print("合格池: n=%d 抽 5:" % len(pool))
for r in g1_pool5:
    print("  ", r["repo"], r["merge_hash"][:10], (r["conformance_score"]["alignment"], r["conformance_score"]["coverage"]))
g1 = [{"src": "held-controversial", "repo": h["repo"], "merge": h["merge"], "align": h.get("v2_align"), "cov": h.get("v2_cov")} for h in g1_controversial] + \
     [{"src": "abcd-A", "repo": s["repo"], "merge": s["merge"], "align": s.get("v2_align"), "cov": s.get("v2_cov")} for s in g1_abcd_a] + \
     [{"src": "topic-pass", "repo": s["repo"], "merge": s["merge"], "align": s.get("v2_align"), "cov": s.get("v2_cov")} for s in g1_topic_pass] + \
     [{"src": "pool-qualified", "repo": r["repo"], "merge": r["merge_hash"], "align": r["conformance_score"]["alignment"], "cov": r["conformance_score"]["coverage"]} for r in g1_pool5]
assert len(g1) == 15, len(g1)

# ---- 格 2 ----
g2 = [{"src": "topic-block", "repo": s["repo"], "merge": s["merge"], "align": s.get("v2_align"), "cov": s.get("v2_cov")}
      for s in samples if s["category"] == "topic" and "应拦" in s["expected"]]
assert len(g2) == 19, len(g2)

out = {"grid1": g1, "grid2": g2, "seed": 42,
       "grid1_note": "争议4 + abcd-A 4 + topic 放行 2 + 合格池抽 5（排除已用键）",
       "grid2_note": "topic 应拦 19（align<=2 或 cov<=2 标签）"}
(BASE / "i4_abc_grid_20260812.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("格 1:", len(g1), "格 2:", len(g2), "→", BASE / "i4_abc_grid_20260812.json")
print("格 1 明细:", [(g["src"], g["repo"], g["merge"][:10], g["align"], g["cov"]) for g in g1])
