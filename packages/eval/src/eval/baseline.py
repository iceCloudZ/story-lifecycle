"""历史基线 — 对 core 集全量 SpecScore + PlanScore。

产出 ``results/baseline_<YYYYMMDD>.json`` + ``results/baseline_<YYYYMMDD>.md``
（总体分布 / 低分 Top10 / 自洽性）。自洽性:随机抽 10 个 artifact 各评 2 次,
维度分差 ≤1 的比例 >80% 才算达标。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import random
from pathlib import Path
from statistics import mean
from typing import Any

from . import dataset
from . import judges

log = logging.getLogger("eval.baseline")

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "results"


def _read_artifact(ds_dir: Path, manifest: dict, doc_key: str) -> str:
    p = dataset.artifact_path(ds_dir, manifest, doc_key)
    return dataset._read_text_robust(p) if p else ""


def score_story(ds_dir: Path, manifest: dict) -> dict[str, Any]:
    """单个 story 的 SpecScore + PlanScore。返回 dict 结果。"""
    prd = _read_artifact(ds_dir, manifest, "prd")
    spec = _read_artifact(ds_dir, manifest, "spec")
    plan = _read_artifact(ds_dir, manifest, "plan")
    template = ""
    tmpl = Path("D:/hc-all/docs/spec-template.md")
    if tmpl.exists():
        template = dataset._read_text_robust(tmpl)
    spec_score = judges.judge_spec(prd, spec, template)
    plan_score = judges.judge_plan(plan, spec) if plan else None
    return {
        "story_key": manifest["story_key"],
        "title": manifest.get("title", ""),
        "profile": manifest.get("profile", ""),
        "spec_score": spec_score.model_dump(),
        "plan_score": plan_score.model_dump() if plan_score else None,
        "model": judges._LLM.client().model,
    }


def run_baseline(
    dataset_dir: str | Path | None = None,
    results_dir: str | Path | None = None,
    limit: int | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """全量跑 core 集 baseline;返回汇总 dict（也写了 json/md）。"""
    ds_dir = Path(dataset_dir) if dataset_dir else Path(__file__).resolve().parent.parent.parent / "dataset"
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    res_dir.mkdir(parents=True, exist_ok=True)

    manifests = dataset.load_manifests(ds_dir, core_only=True)
    if limit:
        manifests = manifests[:limit]
    if not manifests:
        raise RuntimeError("core 集为空——先跑 `eval extract`")

    date = _dt.date.today().strftime("%Y%m%d")
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, mf in enumerate(manifests, 1):
        key = mf["story_key"]
        log.info("[%d/%d] 评分 %s", i, len(manifests), key)
        try:
            results.append(score_story(ds_dir, mf))
        except Exception as e:  # noqa: BLE001 — 单 story 失败不中断
            errors.append(f"{key}: {e}")
            log.exception("评分失败 %s", key)

    # 自洽性抽查:随机 10 个 artifact 各评 2 次
    consistency = _self_consistency(ds_dir, manifests, n=10, seed=seed)

    payload = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "model": judges._LLM.client().model,
        "count": len(results),
        "errors": errors,
        "consistency": consistency,
        "stories": results,
    }
    json_path = res_dir / f"baseline_{date}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md = _render_md(payload, date)
    md_path = res_dir / f"baseline_{date}.md"
    md_path.write_text(md, encoding="utf-8")
    log.info("基线已写入 %s / %s", json_path, md_path)
    return {
        "json": str(json_path),
        "md": str(md_path),
        "count": len(results),
        "errors": errors,
        "consistency": consistency,
    }


def _self_consistency(
    ds_dir: Path, manifests: list[dict], n: int = 10, seed: int = 42
) -> dict[str, Any]:
    """随机 n 个 story 的 spec/plan 各评 2 次,统计维度分差 ≤1 的比例。"""
    rng = random.Random(seed)
    sample = rng.sample(manifests, min(n, len(manifests)))
    pairs: list[dict[str, Any]] = []
    diffs: list[int] = []
    for mf in sample:
        key = mf["story_key"]
        for doc_key, judge_fn in (("spec", judges.judge_spec), ("plan", judges.judge_plan)):
            text = _read_artifact(ds_dir, mf, doc_key)
            prd = _read_artifact(ds_dir, mf, "prd")
            if not text:
                continue
            try:
                if doc_key == "spec":
                    a = judge_fn(prd, text, "")
                    b = judge_fn(prd, text, "")
                else:
                    spec = _read_artifact(ds_dir, mf, "spec")
                    a = judge_fn(text, spec)
                    b = judge_fn(text, spec)
            except Exception as e:  # noqa: BLE001
                log.warning("自洽性抽查失败 %s/%s: %s", key, doc_key, e)
                continue
            dims_a = {k: v for k, v in a.model_dump().items() if isinstance(v, int)}
            dims_b = {k: v for k, v in b.model_dump().items() if isinstance(v, int)}
            for dim, va in dims_a.items():
                vb = dims_b.get(dim, va)
                diff = abs(int(va) - int(vb))
                diffs.append(diff)
                pairs.append({"story_key": key, "doc": doc_key, "dim": dim, "diff": diff})
    total = len(diffs) or 1
    ok = sum(1 for d in diffs if d <= 1)
    return {
        "n_pairs": len(diffs),
        "n_stories": len(sample),
        "diff_le_1_ratio": round(ok / total, 4),
        "mean_diff": round(mean(diffs), 3) if diffs else None,
        "pass_rate_80": (ok / total) > 0.8,
        "details": pairs,
    }


def _dims(spec: dict, plan: dict | None) -> list[tuple[str, int]]:
    out = []
    for d, label in (
        ("completeness", "spec.completeness"),
        ("template_compliance", "spec.template_compliance"),
        ("acceptability", "spec.acceptability"),
    ):
        out.append((label, spec.get(d, 0)))
    if plan:
        for d, label in (
            ("specificity", "plan.specificity"),
            ("spec_alignment", "plan.spec_alignment"),
            ("verifiability", "plan.verifiability"),
        ):
            out.append((label, plan.get(d, 0)))
    return out


def _render_md(payload: dict, date: str) -> str:
    results = payload["stories"]
    con = payload["consistency"]
    lines = [
        f"# Baseline {date}",
        "",
        f"- 模型: `{payload['model']}`",
        f"- 评分 story 数: {payload['count']}（core 集）",
        f"- 失败: {len(payload['errors'])}",
        "",
        "## 总体分布",
        "",
        "| 维度 | 均分 | 中位 | 最小 | 最大 |",
        "|------|------|------|------|------|",
    ]
    by_dim: dict[str, list[int]] = {}
    for r in results:
        for dim, score in _dims(r.get("spec_score") or {}, r.get("plan_score")):
            by_dim.setdefault(dim, []).append(score)
    hist: dict[int, int] = {}
    for dim, vals in sorted(by_dim.items()):
        hist.setdefault(round(mean(vals)), 0)
        for v in vals:
            hist[v] = hist.get(v, 0) + 1
        lines.append(
            f"| {dim} | {mean(vals):.2f} | {sorted(vals)[len(vals)//2]} | {min(vals)} | {max(vals)} |"
        )
    lines += [
        "",
        "| 分数 | 出现次数 |",
        "|------|----------|",
    ]
    for s in sorted(hist):
        lines.append(f"| {s} | {hist[s]} |")
    lines += ["", "## 低分 case Top 10（按 spec 三唯 + plan 三唯合计）", ""]
    ranked = sorted(
        results,
        key=lambda r: sum(s for _, s in _dims(r.get("spec_score") or {}, r.get("plan_score"))),
    )
    for r in ranked[:10]:
        ss = r.get("spec_score") or {}
        ps = r.get("plan_score") or {}
        total = sum(s for _, s in _dims(ss, ps))
        lines += [
            f"### {r['story_key']} — {r.get('title','')[:40]}（合计 {total}/30）",
            f"- spec: completeness={ss.get('completeness')}, template={ss.get('template_compliance')}, "
            f"acceptability={ss.get('acceptability')}",
            f"- plan: specificity={ps.get('specificity')}, alignment={ps.get('spec_alignment')}, "
            f"verifiability={ps.get('verifiability')}" if ps else "- plan: 无",
        ]
        for f in (ss.get("findings") or [])[:3]:
            lines.append(f"  - {f}")
        if ps:
            for f in (ps.get("findings") or [])[:2]:
                lines.append(f"  - {f}")
    lines += [
        "",
        "## 自洽性",
        "",
        f"- 抽查对: {con['n_pairs']}（{con['n_stories']} 个 story 的 spec/plan 各 2 次）",
        f"- 维度分差 ≤1 比例: **{con['diff_le_1_ratio']:.1%}**（要求 >80%）",
        f"- 平均分差: {con['mean_diff']}",
        f"- 达标: {'✅' if con['pass_rate_80'] else '❌'}",
        "",
    ]
    if payload["errors"]:
        lines += ["## 失败列表", ""]
        lines += [f"- {e}" for e in payload["errors"]]
    return "\n".join(lines) + "\n"
