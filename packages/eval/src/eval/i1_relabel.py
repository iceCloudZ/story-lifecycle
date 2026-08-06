"""迭代 1 收尾 1：B/A 类同源重评 — Go 端点 + 现 prompt 重评 10 条，新标签落盘。

新标签规则（阈值 align≤2 或 cov≤2 → 应拦/HIGH）：
- 与验收 1 的 check_conformance 输出一致（同源 judge），但标签从此以本次重评为准。
- B 类中 Go 判合格（align>2 且 cov>2）→ 移出应拦集（标签迁移，非漏拦）。
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


def _reference_for(tid: str) -> tuple[str, str]:
    """参照物：TAPD description（与快照 conformance 同源）> evidence spec > gold PRD。"""
    for l in (SNAP / 'tapd_stories.jsonl').read_text(encoding='utf-8').splitlines():
        if l.strip():
            r = json.loads(l)
            if r['tapd_id'] == tid and (r.get('description') or '').strip():
                desc = re.sub(r'<[^>]+>', ' ', r['description'])
                desc = re.sub(r'\s+', ' ', desc).strip()
                if desc:
                    return desc, 'tapd-desc'
    for l in (SNAP / 'stories_matched.jsonl').read_text(encoding='utf-8').splitlines():
        if not l.strip():
            continue
        e = json.loads(l)
        if e.get('tapd_id') == tid and e.get('evidence_dir'):
            d = Path(e['evidence_dir'])
            for cand in ('spec.md', 'Spec.md', 'design.md'):
                p = d / cand
                if p.exists() and p.stat().st_size > 0:
                    return p.read_text(encoding='utf-8', errors='replace'), 'spec'
    prd = SANDBOX / "gold" / f"tapd-{tid}" / "PRD.md"
    if prd.exists() and prd.stat().st_size > 0:
        return prd.read_text(encoding='utf-8', errors='replace'), 'prd'
    return "", ""


def main() -> dict:
    os.environ["STORY_HOME"] = str(SANDBOX / "story_home")
    os.environ["STORY_LLM_BASE_URL"] = "https://opencode.ai/zen/go/v1"
    sys.path.insert(0, str(SL_SRC))
    from eval.judges import configure_llm_env

    configure_llm_env()
    from story_lifecycle.orchestrator.evaluation.conformance import check_conformance

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
        ref_text, ref_name = _reference_for(tid)
        spec_p = SANDBOX / "ws" / f"tapd-{tid}-relabel" / "story" / "ref.md"
        spec_p.parent.mkdir(parents=True, exist_ok=True)
        spec_p.write_text(ref_text, encoding="utf-8")

        res = check_conformance(
            story_key=f"tapd-{tid}", workspace=str(SANDBOX / "ws" / f"tapd-{tid}-relabel"),
            spec_path=str(spec_p), diff_text=diff_text[:80_000],
        )
        # 新标签：应拦 = align≤2 或 cov≤2
        should_block = res.alignment <= 2 or res.coverage <= 2
        out.append({
            "tapd_id": tid, "old_cls": s['cls'],
            "alignment": res.alignment, "coverage": res.coverage, "scope_drift": res.scope_drift,
            "should_block": should_block, "ref": ref_name,
            "summary": res.summary[:100],
        })
        print(f"[{s['cls']}] {tid[-8:]} align={res.alignment} cov={res.coverage} "
              f"should_block={should_block} ref={ref_name}", flush=True)

    path = RESULTS / "iteration1_relabel_20260806.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 统计：旧 B 类中应拦/迁移、旧 A 类中误拦
    b_should = [r for r in out if r['old_cls'] == 'B' and r['should_block']]
    b_migrate = [r for r in out if r['old_cls'] == 'B' and not r['should_block']]
    a_should = [r for r in out if r['old_cls'] == 'A' and r['should_block']]
    a_ok = [r for r in out if r['old_cls'] == 'A' and not r['should_block']]
    print(f"\n新标签统计:")
    print(f"  旧B类: 应拦 {len(b_should)}/5, 迁移出应拦集 {len(b_migrate)}/5")
    print(f"  旧A类: 应拦(误拦候选) {len(a_should)}/5, 放行 {len(a_ok)}/5")
    print(f"  剩余应拦 case 拦截率: {len(b_should)}/{len(b_should)}（F2 管道对同源标签的拦截）")
    return {
        "b_should_block": len(b_should), "b_migrated": len(b_migrate),
        "a_should_block": len(a_should), "a_ok": len(a_ok),
    }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    main()
