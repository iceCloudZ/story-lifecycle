"""迭代 1 验收 1 v4：F2 conformance 精确度量（参照物与快照同口径）。

- 参照物优先级：evidence spec > gold PRD > TAPD description（与快照 conformance 同源）
- B 类 5 条 → 期望拦截（align≤2）≥4/5
- A 类 5 条 → 期望误拦 ≤1/5
"""
import json
import os
import re
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = PACKAGE_ROOT / "sandbox"
RESULTS = PACKAGE_ROOT / "results"
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"
SNAP = PACKAGE_ROOT / "dataset" / "snapshot_20260805"


def _files_from_diff(diff_text: str) -> list[str]:
    files = []
    for m in re.finditer(r"^diff --git a/(.+?) b/", diff_text, re.M):
        f = m.group(1)
        if f not in files:
            files.append(f)
    return files


def _reference_for(tid: str) -> tuple[str, Path]:
    """参照物：TAPD description（与快照 conformance 同源）> evidence spec > gold PRD。"""
    # TAPD description（快照原始）
    for l in (SNAP / 'tapd_stories.jsonl').read_text(encoding='utf-8').splitlines():
        if l.strip():
            r = json.loads(l)
            if r['tapd_id'] == tid and (r.get('description') or '').strip():
                import re as _re
                desc = _re.sub(r'<[^>]+>', ' ', r['description'])
                desc = _re.sub(r'\s+', ' ', desc).strip()
                if desc:
                    return desc, Path('tapd-desc')
    # evidence spec（快照 entity evidence_dir）
    for l in (SNAP / 'stories_matched.jsonl').read_text(encoding='utf-8').splitlines():
        if not l.strip():
            continue
        e = json.loads(l)
        if e.get('tapd_id') == tid and e.get('evidence_dir'):
            d = Path(e['evidence_dir'])
            for cand in ('spec.md', 'Spec.md', 'design.md'):
                p = d / cand
                if p.exists() and p.stat().st_size > 0:
                    return p.read_text(encoding='utf-8', errors='replace'), p
    # gold PRD
    prd = SANDBOX / "gold" / f"tapd-{tid}" / "PRD.md"
    if prd.exists() and prd.stat().st_size > 0:
        return prd.read_text(encoding='utf-8', errors='replace'), prd
    return "", Path('')


def main() -> dict:
    os.environ["STORY_HOME"] = str(SANDBOX / "story_home")
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"
    sys.path.insert(0, str(SL_SRC))
    from eval.judges import configure_llm_env

    configure_llm_env()
    from story_lifecycle.orchestrator.evaluation.conformance import (
        check_conformance,
        inject_conformance_findings,
    )

    samples = json.loads(Path('C:/Users/zzh58/AppData/Local/Temp/opencode/final20.json').read_text(encoding='utf-8'))
    b = [s for s in samples if s['cls'] == 'B']
    a = [s for s in samples if s['cls'] == 'A']

    out = []
    for s in b + a:
        tid = s['tapd_id']
        gold = SANDBOX / "gold" / s['story_key']
        diff_path = gold / "delivery.diff"
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace") if diff_path.exists() else ""
        files = _files_from_diff(diff_text)

        ref_text, ref_path = _reference_for(tid)
        # 落参照物文件（conformance 按 spec_path 读）
        spec_p = SANDBOX / "ws" / f"tapd-{tid}-i1v4" / "story" / "ref.md"
        spec_p.parent.mkdir(parents=True, exist_ok=True)
        spec_p.write_text(ref_text, encoding="utf-8")

        res = check_conformance(
            story_key=f"tapd-{tid}", workspace=str(SANDBOX / "ws" / f"tapd-{tid}-i1v4"),
            spec_path=str(spec_p), diff_text=diff_text[:80_000],
        )
        findings = inject_conformance_findings(res)
        blocked = res.alignment <= 2
        out.append({
            "tapd_id": tid, "cls": s['cls'], "n_files": len(files),
            "alignment": res.alignment, "coverage": res.coverage, "scope_drift": res.scope_drift,
            "blocked": blocked, "findings": findings, "summary": res.summary[:100],
            "ref": Path(ref_path).name if isinstance(ref_path, Path) else ref_path,
        })
        print(f"[{s['cls']}] {tid[-8:]} align={res.alignment} cov={res.coverage} "
              f"blocked={blocked} ref={out[-1]['ref'][:20]}", flush=True)

    path = RESULTS / "iteration1_acceptance1.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    b_blocked = sum(1 for r in out if r['cls'] == 'B' and r['blocked'])
    a_blocked = sum(1 for r in out if r['cls'] == 'A' and r['blocked'])
    print(f"B 拦截 {b_blocked}/5 | A 误拦 {a_blocked}/5")
    return {"b_blocked": b_blocked, "a_blocked": a_blocked, "n": len(out)}


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
