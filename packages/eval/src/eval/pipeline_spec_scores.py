"""round2 生成 spec 质量评分：ConformanceScore 同款 prompt，参照物 = gold PRD。

产出：results/pipeline_spec_scores_20260805.json + 追加到 md 报告 §2。
"""
import concurrent.futures
import json
import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
RESULTS = PACKAGE_ROOT / "results"


def main() -> dict:
    rows = json.loads(Path('C:/Users/zzh58/AppData/Local/Temp/opencode/final20.json').read_text(encoding='utf-8'))
    from eval.judges import configure_llm_env, _LLM

    configure_llm_env()
    concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", "8"))

    def score_one(r):
        if not r.get("spec"):
            return {**r, "spec_align": None, "spec_score_err": "no spec"}
        gold_prd = SANDBOX / "gold" / r["story_key"] / "PRD.md"
        ref = gold_prd.read_text(encoding="utf-8", errors="replace") if gold_prd.exists() else ""
        spec_text = Path(r["spec"]).read_text(encoding="utf-8", errors="replace")
        try:
            from eval.judges import judge_conformance

            cs = judge_conformance(ref, "prd", spec_text)
            return {**r, "spec_align": cs.alignment, "spec_cov": cs.coverage,
                    "spec_drift": cs.scope_drift, "spec_summary": cs.summary[:150]}
        except Exception as e:
            return {**r, "spec_align": None, "spec_score_err": str(e)[:150]}

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(score_one, r) for r in rows]
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            out.append(fut.result())
            if i % 10 == 0 or i == len(rows):
                print(f"评分进度 {i}/{len(rows)}", file=sys.stderr)
    out.sort(key=lambda x: x['tapd_id'])

    path = RESULTS / "pipeline_spec_scores_20260805.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    from collections import defaultdict
    by_cls = defaultdict(list)
    for r in out:
        if r.get("spec_align") is not None:
            by_cls[r["cls"]].append(r["spec_align"])
    print("=== 生成 spec alignment 均分（分类别） ===")
    total = []
    for k in ("A", "B", "C", "D"):
        vals = by_cls.get(k, [])
        if vals:
            m = sum(vals) / len(vals)
            total += vals
            print(f"  {k}: {m:.2f} ({len(vals)} 条, 值 {sorted(vals)})")
    if total:
        print(f"  全部: {sum(total)/len(total):.2f} ({len(total)} 条)")
    return {"n": len(out), "scored": sum(1 for r in out if r.get("spec_align") is not None)}


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
