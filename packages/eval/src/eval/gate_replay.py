"""gate 回测 — 用快照语料测量 story-lifecycle unified gate 的历史拦截率。

只读 import story-lifecycle 的 unified_gate；STORY_HOME 指向临时目录隔离 DB；
LLM 端点与快照一致（DeepSeek 官方）。产出 results/gate_replay_20260805.md。
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
RESULTS = PACKAGE_ROOT / "results"

# story-lifecycle 源码路径（只读 import）
SL_SRC = PACKAGE_ROOT.parent / "packages" / "story-lifecycle" / "src"

HC_ALL = Path("D:/hc-all")


def _load_rows() -> list[dict]:
    return [
        json.loads(l)
        for l in (SNAP / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def _load_deliveries() -> dict[tuple[str, str], dict]:
    out = {}
    for l in (SNAP / "deliveries.jsonl").read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        d = json.loads(l)
        out[(d["repo"], d["merge_hash"])] = d
    return out


def _repo_path(repo: str) -> Path | None:
    p = HC_ALL / "frontends" / "hc-admin" if repo == "hc-admin" else HC_ALL / repo
    return p if p.is_dir() else None


def _diff_text(repo: str, merge_hash: str, max_chars: int = 60_000) -> str:
    rp = _repo_path(repo)
    if rp is None:
        return ""
    base = f"{merge_hash}^1"
    r = subprocess.run(
        ["git", "--no-pager", "diff", "-U2", base, merge_hash],
        cwd=str(rp), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    if r.returncode != 0:
        return ""
    return r.stdout[:max_chars]


def _build_done_data(row: dict, delivery: dict) -> dict:
    """把历史 merge + 参照物差异适配成 gate 的 done_data（agent 交付自述）。"""
    cf = row.get("conformance_score") or {}
    findings = cf.get("findings") or []
    summary = cf.get("summary") or ""
    # files_changed 从 diffstat 取
    files = []
    diffstat = delivery.get("diffstat") or {}
    if isinstance(diffstat, dict):
        f = diffstat.get("files")
        if isinstance(f, list):
            files = f[:20]
        elif isinstance(f, str):
            files = [f]
        else:
            files = [str(k) for k in list(diffstat.keys())[:20]] or []
    elif isinstance(diffstat, str):
        files = [diffstat]
    return {
        "summary": summary,
        "files_changed": files,
        "findings_hint": findings,
    }


# ---- 参照物重建（只读快照，供存档） ----


def _snap_entities() -> dict[str, dict]:
    out = {}
    for l in (SNAP / "stories_matched.jsonl").read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        e = json.loads(l)
        if e.get("tapd_id"):
            out[e["tapd_id"]] = e
    return out


def _snap_tapd() -> dict[str, dict]:
    out = {}
    for l in (SNAP / "tapd_stories.jsonl").read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        r = json.loads(l)
        out[r["tapd_id"]] = r
    return out


def _read_text_robust(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _reference_for(row: dict, entities: dict, tapd: dict, story_refs_dir: Path) -> tuple[str, str]:
    """参照物重建：spec > prd > story_refs > tapd 描述（与 scanall 同序）。"""
    tid = row.get("tapd_id") or ""
    ent = entities.get(tid) or {}
    ed = ent.get("evidence_dir") or ""
    if ed:
        d = Path(ed)
        for doc_key, cands in (("spec", ["spec.md", "Spec.md", "design.md"]),
                               ("prd", ["PRD.md", "prd.md", "Prd.md"])):
            for cand in cands:
                f = d / cand
                if f.exists() and f.stat().st_size > 0:
                    return _read_text_robust(f), doc_key
    # story_refs：快照里 link-only 富化
    from eval.ref_fetch import is_link_only, load_story_ref

    rec = tapd.get(tid) or {}
    desc = rec.get("description") or ""
    if is_link_only(desc):
        ref = _read_text_robust(story_refs_dir / f"{tid}.md")
        if len(ref) >= 100:
            return ref, "story_refs"
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", desc)
    text = _re.sub(r"\s+", " ", text).strip()
    return (text, "tapd") if text else ("", "")


def _archive_refs(samples: list[tuple[str, dict]], refs_dir: Path) -> None:
    """逐样本存档参照物原文（results/gate_replay_refs_20260805/）。"""
    entities = _snap_entities()
    tapd = _snap_tapd()
    refs_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for kind, r in samples:
        ref, ref_type = _reference_for(r, entities, tapd, SNAP / "story_refs")
        name = f"{kind}_{r['repo']}_{r['merge_hash'][:10]}_{r.get('tapd_id','none')[:8]}"
        (refs_dir / f"{name}.md").write_text(
            f"# 参照物（{ref_type}）\n\n- merge: {r['repo']}:{r['merge_hash']}\n"
            f"- tapd: {r.get('tapd_id','')}\n- story_key: {r.get('story_key','')}\n"
            f"- eval alignment: {r['conformance_score']['alignment']}\n\n---\n\n{ref[:120_000] or '（无参照物）'}",
            encoding="utf-8",
        )
        n += 1
    print(f"参照物存档: {n} 个 → {refs_dir}", file=sys.stderr)


def _run_gate_one(row: dict, delivery: dict, tmp_home: Path) -> dict:
    """调用 unified gate 一次，返回结构化结果。"""
    sys.path.insert(0, str(SL_SRC))
    os.environ["STORY_HOME"] = str(tmp_home)
    # LLM 端点：opencode-go（https://opencode.ai/zen/go/v1，deepseek-v4-flash）
    from eval.judges import configure_llm_env

    configure_llm_env()
    from story_lifecycle.orchestrator.evaluation.unified_gate import (
        run_unified_verify_gate,
    )

    done = _build_done_data(row, delivery)
    story_key = f"replay-gate-{row['repo']}-{row['merge_hash'][:8]}"
    t0 = time.monotonic()
    try:
        result = run_unified_verify_gate(
            story_key=story_key,
            stage="verify",
            workspace=str(tmp_home),
            context={"task_type": ""},
            done_data=done,
            adapter_name="opencode",
            retry_count=1,
        )
        elapsed = time.monotonic() - t0
        return {
            "gate_verdict": result.get("verdict"),
            "gate_decision": result.get("decision"),
            "gate_reason": (result.get("reason") or "")[:200],
            "gate_findings": result.get("findings") or [],
            "gate_repair": (result.get("repair_action") or {}),
            "elapsed_s": round(elapsed, 1),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200], "elapsed_s": round(time.monotonic() - t0, 1)}


def main() -> dict:
    rows = _load_rows()
    deliveries = _load_deliveries()

    pos = [r for r in rows if (r.get("conformance_score") or {}).get("alignment") is not None
           and r["conformance_score"]["alignment"] <= 2]
    neg_pool = [r for r in rows if (r.get("conformance_score") or {}).get("alignment") is not None
                and r["conformance_score"]["alignment"] >= 4]
    # 对照取与正样本等量（池子不足则全取）
    rng = random.Random(42)
    neg = rng.sample(neg_pool, min(len(neg_pool), len(pos)))

    samples = [("pos", r) for r in pos] + [("neg", r) for r in neg]
    print(f"样本: 正 {len(pos)} / 对照 {len(neg)}（对照池 {len(neg_pool)}）", file=sys.stderr)

    # 参照物原文逐样本存档
    _archive_refs(samples, RESULTS / "gate_replay_refs_20260805")

    out = []
    tmp_home = Path(tempfile.mkdtemp(prefix="gate_replay_"))

    def _worker(args):
        idx, (kind, r) = args
        dl = deliveries.get((r["repo"], r["merge_hash"]))
        if dl is None:
            return {"idx": idx, "kind": kind, "repo": r["repo"], "merge": r["merge_hash"][:10],
                    "tapd": r.get("tapd_id", ""), "alignment": r["conformance_score"]["alignment"],
                    "skip": "deliveries 缺失"}
        rec = _run_gate_one(r, dl, tmp_home)
        rec.update({
            "idx": idx, "kind": kind, "repo": r["repo"], "merge": r["merge_hash"][:10],
            "tapd": r.get("tapd_id", ""), "story_key": r.get("story_key", ""),
            "alignment": r["conformance_score"]["alignment"],
            "eval_findings": (r["conformance_score"].get("findings") or [])[:3],
        })
        return rec

    import concurrent.futures

    CONCURRENCY = int(os.environ.get("EVAL_LLM_CONCURRENCY", "8"))
    done_n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [executor.submit(_worker, (i, s)) for i, s in enumerate(samples)]
        for fut in concurrent.futures.as_completed(futures):
            out.append(fut.result())
            done_n += 1
            if done_n % 20 == 0 or done_n == len(samples):
                print(f"进度 {done_n}/{len(samples)}（并发 {CONCURRENCY}）", file=sys.stderr)

    out.sort(key=lambda x: x["idx"])

    _render_report(out, pos, neg)
    return {"total": len(out), "pos": len(pos), "neg": len(neg)}


def _version_header() -> list[str]:
    """被测版本：story-lifecycle HEAD + git status 摘要 + hc-all repos HEAD。"""
    def sh(cmd, cwd):
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
            return r.stdout.strip()
        except Exception:
            return "ERR"

    sl = PACKAGE_ROOT.parent
    lines = ["## 被测版本（工作区现状，无 worktree 隔离）", ""]
    lines.append(f"- story-lifecycle HEAD: {sh(['git', 'rev-parse', 'HEAD'], sl)[:12]} "
                 f"({sh(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], sl)})")
    status = sh(['git', 'status', '--short'], sl).splitlines()
    lines.append(f"- story-lifecycle git status: {len(status)} 个改动文件"
                 + (f"（示例: {status[0].strip()[:60]} 等）" if status else "（干净）"))
    lines.append("- hc-all 涉及 repo HEAD:")
    hc = Path("D:/hc-all")
    repos = sorted([d.name for d in hc.iterdir() if d.is_dir() and (d / ".git").exists()])
    repos.append("frontends/hc-admin")
    for name in repos:
        p = hc / name if name != "frontends/hc-admin" else hc / "frontends" / "hc-admin"
        head = sh(['git', 'rev-parse', 'HEAD'], p)[:12]
        branch = sh(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], p)
        dirty = len([l for l in sh(['git', 'status', '--porcelain'], p).splitlines() if l.strip()])
        lines.append(f"  - {name}: {head} ({branch}) dirty={dirty}")
    lines.append("")
    return lines


def _render_report(rows: list[dict], pos: list[dict], neg: list[dict]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    lines = ["# gate 回测报告 20260805（基于 snapshot_20260805）", ""]
    lines += _version_header()

    # 统计
    def _verdict_of(r: dict) -> str:
        if r.get("skip"):
            return "skip"
        d = r.get("gate_decision")
        return "block" if d in ("retry", "fail") else ("pass" if d == "advance" else "unknown")

    pos_rows = [r for r in rows if r["kind"] == "pos"]
    neg_rows = [r for r in rows if r["kind"] == "neg"]
    pos_run = [r for r in pos_rows if not r.get("skip")]
    neg_run = [r for r in neg_rows if not r.get("skip")]
    pos_block = [r for r in pos_run if _verdict_of(r) == "block"]
    neg_block = [r for r in neg_run if _verdict_of(r) == "block"]
    pos_pass = [r for r in pos_run if _verdict_of(r) == "pass"]

    skip_reasons = {}
    for r in rows:
        if r.get("skip"):
            skip_reasons[r["skip"]] = skip_reasons.get(r["skip"], 0) + 1

    lines += [
        "## 总表",
        "",
        f"- 正样本（应拦截，alignment≤2）: {len(pos)} 条，运行 {len(pos_run)}，拦截 {len(pos_block)}",
        f"  - **拦截率: {len(pos_block)/max(len(pos_run),1):.1%}**",
        f"- 对照（不应拦截，alignment≥4）: {len(neg)} 条，运行 {len(neg_run)}，误拦 {len(neg_block)}",
        f"  - **误拦率: {len(neg_block)/max(len(neg_run),1):.1%}**",
        f"- skip: {len(rows)-len(pos_run)-len(neg_run)} 条，原因分布: {skip_reasons or '无'}",
        "",
        "## 明细表",
        "",
        "| # | 类型 | repo | merge | tapd | eval align | gate verdict | gate decision | gate findings | 一致? |",
        "|---|------|------|-------|-----|-----------|--------------|---------------|---------------|-------|",
    ]
    for i, r in enumerate(rows, 1):
        if r.get("skip"):
            lines.append(f"| {i} | {r['kind']} | {r['repo']} | {r['merge']} | {r.get('tapd','')[:8]} | {r['alignment']} | skip | {r['skip']} | - | - |")
            continue
        v = _verdict_of(r)
        consistent = "是" if ((r["kind"] == "pos" and v == "block") or (r["kind"] == "neg" and v == "pass")) else "否"
        nf = len(r.get("gate_findings") or [])
        lines.append(
            f"| {i} | {r['kind']} | {r['repo']} | {r['merge']} | {r.get('tapd','')[:8]} | {r['alignment']} | "
            f"{r.get('gate_verdict','?')} | {r.get('gate_decision','?')} | {nf} | {consistent} |"
        )

    # 不一致 case 分析：漏拦（pos 放行）挑 5，误拦（neg 拦截）挑 3
    lines += ["", "## 不一致 case 分析", ""]
    missed = [r for r in pos_run if _verdict_of(r) == "pass"]
    false_pos = [r for r in neg_run if _verdict_of(r) == "block"]
    lines.append(f"### 漏拦（gate 放行 drift）{len(missed)} 条，挑 5 条")
    for r in missed[:5]:
        lines.append(f"- **{r['repo']}:{r['merge']}** align={r['alignment']} tapd={r.get('tapd','')[:8]}")
        lines.append(f"  - gate reason: {r.get('gate_reason','')[:150]}")
        lines.append(f"  - eval findings: {'; '.join(r.get('eval_findings') or [])[:200]}")
    lines.append("")
    lines.append(f"### 误拦（gate 拦截对照）{len(false_pos)} 条，挑 3 条")
    for r in false_pos[:3]:
        lines.append(f"- **{r['repo']}:{r['merge']}** align={r['alignment']} tapd={r.get('tapd','')[:8]}")
        lines.append(f"  - gate reason: {r.get('gate_reason','')[:150]}")
    lines.append("")

    path = RESULTS / "gate_replay_20260805.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告: {path}", file=sys.stderr)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    import logging
    res = main()
    print(json.dumps(res, ensure_ascii=False))
