"""B 类注入重跑（round2 §3.3 修正）：对 B 类 5 条不重跑 design，
只注入完整历史 diff 重跑 verify（gate 直接调用）。

- done_data.files_changed = 真实 merge 的文件清单（从 delivery.diff 的 diff --git 行提取）
- done_data.delivery_diff = delivery.diff 全文（历史 merge 真实交付）
- 走 run_unified_verify_gate（Go 端点）
"""
import json
import os
import re
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
RESULTS = PACKAGE_ROOT / "results"
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"


def _files_from_diff(diff_text: str) -> list[str]:
    """从 diff 的 'diff --git a/X b/X' 行提取文件清单（去重）。"""
    files = []
    for m in re.finditer(r"^diff --git a/(.+?) b/", diff_text, re.M):
        f = m.group(1)
        if f not in files:
            files.append(f)
    return files


def main() -> dict:
    os.environ["STORY_HOME"] = str(SANDBOX / "story_home")
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"
    sys.path.insert(0, str(SL_SRC))
    from eval.judges import configure_llm_env

    configure_llm_env()
    from story_lifecycle.orchestrator.evaluation.unified_gate import run_unified_verify_gate

    samples = json.loads(Path('C:/Users/zzh58/AppData/Local/Temp/opencode/final20.json').read_text(encoding='utf-8'))
    gold_info = {s['tapd_id']: s for s in json.loads(Path('sandbox/gold/samples20.json').read_text(encoding='utf-8'))}
    b = [s for s in samples if s['cls'] == 'B']
    print(f"B 类 {len(b)} 条注入重跑", flush=True)

    out_rows = []
    for s in b:
        tid = s['tapd_id']
        sk = s['story_key']
        gold = SANDBOX / "gold" / sk
        diff_path = gold / "delivery.diff"
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        files = _files_from_diff(diff_text)
        # 参照物 = gold PRD（gate 的 verify 摘要用 spec，但 B 类衡量拦截用真实 diff）
        prd = gold / "PRD.md"
        prd_text = prd.read_text(encoding="utf-8", errors="replace")[:60_000]

        done_data = {
            "summary": f"历史真实交付回放验证（merge diff 注入）\n\n需求参照物:\n{prd_text[:3000]}",
            "files_changed": files,
            "delivery_diff": diff_text[:80_000],
        }
        t0 = time.monotonic()
        try:
            result = run_unified_verify_gate(
                story_key=sk, stage="verify", workspace=str(SANDBOX / "ws" / sk),
                context={"task_type": ""}, done_data=done_data,
                adapter_name="opencode", retry_count=1,
            )
            rec = {
                "tapd_id": tid, "story_key": sk, "cls": "B",
                "merges": (gold_info.get(tid) or {}).get('merges', []), "n_files": len(files),
                "gate_verdict": result.get("verdict"),
                "gate_decision": result.get("decision"),
                "gate_reason": (result.get("reason") or "")[:300],
                "gate_findings": result.get("findings") or [],
                "elapsed_s": round(time.monotonic() - t0, 1),
            }
        except Exception as e:
            rec = {
                "tapd_id": tid, "story_key": sk, "cls": "B",
                "gate_error": f"{e.__class__.__name__}: {e}"[:200],
                "elapsed_s": round(time.monotonic() - t0, 1),
            }
        out_rows.append(rec)
        print(f"  {tid[-8:]} files={len(files)} gate={rec.get('gate_decision','ERR')} "
              f"{rec.get('gate_verdict','')} findings={len(rec.get('gate_findings') or [])}", flush=True)

    path = RESULTS / "pipeline_b_injected_20260805.json"
    path.write_text(json.dumps(out_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    block = sum(1 for r in out_rows if r.get("gate_decision") in ("retry", "fail"))
    print(f"B 类注入重跑: 拦截 {block}/{len(out_rows)}")
    return {"n": len(out_rows), "blocked": block}


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
