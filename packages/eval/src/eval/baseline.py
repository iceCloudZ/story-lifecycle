"""历史基线 — 对全量入选集打分（SpecScore/PlanScore/DeliveryScore/ConformanceScore）。

产出 ``results/baseline_<YYYYMMDD>.json`` + ``results/baseline_<YYYYMMDD>.md``:
总体分布 / 低分 Top10 / 自洽性 / spec-代码漂移 case 列表。

ConformanceScore 参照物优先级: C 源 spec > C 源 PRD > B 源 TAPD 描述。
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import logging
import os
import random
import threading
from pathlib import Path
from statistics import mean
from typing import Any

from . import dataset
from . import judges

log = logging.getLogger("eval.baseline")

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "results"
DATASET_DIR = Path(__file__).resolve().parent.parent.parent / "dataset"
PARTIAL_NAME = "baseline_partial.jsonl"


def _load_partial(res_dir: Path) -> dict[str, dict]:
    """读已有的 baseline_partial.jsonl,返回 {tapd_id: 打分记录}。"""
    done: dict[str, dict] = {}
    p = res_dir / PARTIAL_NAME
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec.get("tapd_id") or rec.get("story_key") or "?"] = rec
    return done


def _append_partial(res_dir: Path, rec: dict) -> None:
    """每打完一个 story 立即追加一行,中断不丢。"""
    p = res_dir / PARTIAL_NAME
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_artifact(ds_dir: Path, manifest: dict, doc_key: str) -> str:
    p = dataset.artifact_path(ds_dir, manifest, doc_key)
    return dataset._read_text_robust(p) if p else ""


def _reference_for(manifest: dict, ds_dir: Path) -> tuple[str, str]:
    """按优先级取需求参照物: spec > PRD > TAPD 描述。返回 (text, type)。"""
    spec = _read_artifact(ds_dir, manifest, "spec")
    if spec:
        return spec, "spec"
    prd = _read_artifact(ds_dir, manifest, "prd")
    if prd:
        return prd, "prd"
    return "", "tapd"


def _tapd_description(manifest: dict) -> str:
    """从 tapd_stories.jsonl 缓存取 TAPD 需求描述（strip HTML）。"""
    tid = manifest.get("tapd_id") or ""
    if not tid:
        return ""
    cache = _tapd_cache()
    rec = cache.get(tid) or {}
    desc = rec.get("description") or ""
    return _strip_html(desc)[:40_000]


_tapd_cache_data: dict | None = None


def _tapd_cache() -> dict:
    global _tapd_cache_data
    if _tapd_cache_data is None:
        _tapd_cache_data = {}
        p = DATASET_DIR / "tapd_stories.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    _tapd_cache_data[rec["tapd_id"]] = rec
    return _tapd_cache_data


def _strip_html(text: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _delivery_diff_texts(manifest: dict) -> dict[str, str]:
    """收集该 story 各交付的 diff 全文（core 集已落盘;未落的现拉,存 dataset/diffs）。"""
    texts: dict[str, str] = {}
    out_dir = DATASET_DIR / (manifest.get("dir") or "") / "diffs"
    for dl in manifest.get("deliveries", []):
        if dl.get("confidence") not in ("high", "official"):
            continue
        fn = f"{dl['repo']}_{dl['merge_hash']}.diff"
        p = out_dir / fn
        if not p.exists() or p.stat().st_size == 0:
            r = dataset._write_diff(dl["repo"], dl["merge_hash"], out_dir)
            if r.get("bytes") is None:
                continue
        if p.exists():
            texts[f"{dl['repo']}_{dl['merge_hash'][:10]}"] = dataset._read_text_robust(p, limit=150_000)
    return texts


def score_manifest(ds_dir: Path, manifest: dict, with_diffs: bool = True) -> dict[str, Any]:
    """单个实体全维度打分。返回 dict 结果。"""
    out: dict[str, Any] = {
        "tapd_id": manifest.get("tapd_id", ""),
        "story_key": manifest.get("story_key", ""),
        "title": manifest.get("title", ""),
        "docs": list((manifest.get("docs") or {}).keys()),
    }
    prd = _read_artifact(ds_dir, manifest, "prd")
    spec = _read_artifact(ds_dir, manifest, "spec")
    plan = _read_artifact(ds_dir, manifest, "plan")
    template = ""
    tmpl = Path(os.environ.get("EVAL_SPEC_TEMPLATE") or "D:/hc-all/docs/spec-template.md")
    if tmpl.exists():
        template = dataset._read_text_robust(tmpl)

    if spec:
        out["spec_score"] = judges.judge_spec(prd, spec, template).model_dump()
    if plan and spec:
        out["plan_score"] = judges.judge_plan(plan, spec).model_dump()

    # DeliveryScore:有交付的实体
    deliveries = manifest.get("deliveries", [])
    if deliveries:
        dscore = judges.judge_delivery(
            [c for dl in deliveries for c in dl.get("commits", [])][:80],
            repo=",".join(sorted({dl["repo"] for dl in deliveries})),
            branch=deliveries[0].get("branch", ""),
        )
        out["delivery_score"] = dscore.model_dump()

    # ConformanceScore:diff vs 参照物（管线外 story 用 TAPD 描述/story_refs 兜底）
    if with_diffs and deliveries:
        diff_texts = _delivery_diff_texts(manifest)
        if diff_texts:
            ref, ref_type = _reference_for(manifest, ds_dir)
            if not ref:
                # 参照物优先级: C 源 spec > C 源 PRD > story_refs > TAPD 描述
                from .ref_fetch import reference_for_tapd

                ref, ref_type = reference_for_tapd(_tapd_cache(), manifest.get("tapd_id") or "")
                if not ref:
                    log.info("%s: 无任何参照物,跳过 Conformance", manifest.get("dir"))
                    return out
            combined_diff = "\n\n".join(f"### {k}\n{diff_texts[k][:80_000]}" for k in list(diff_texts)[:6])
            try:
                cscore = judges.judge_conformance(ref, ref_type, combined_diff)
                out["conformance_score"] = cscore.model_dump()
            except Exception as e:  # noqa: BLE001
                out["conformance_error"] = str(e)
                log.warning("Conformance 打分失败 %s: %s", manifest.get("dir"), e)
    return out


def run_baseline(
    dataset_dir: str | Path | None = None,
    results_dir: str | Path | None = None,
    limit: int | None = None,
    seed: int = 42,
    with_diffs: bool = True,
    force: bool = False,
    concurrency: int | None = None,
) -> dict[str, Any]:
    ds_dir = Path(dataset_dir) if dataset_dir else DATASET_DIR
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    res_dir.mkdir(parents=True, exist_ok=True)

    manifests = dataset.load_manifests(ds_dir)
    if limit:
        manifests = manifests[:limit]
    if not manifests:
        raise RuntimeError("入选集为空——先跑 `eval link` + `eval extract`")

    if concurrency is None:
        concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", "4"))
    if concurrency < 1:
        concurrency = 1

    date = _dt.date.today().strftime("%Y%m%d")
    partial_path = res_dir / PARTIAL_NAME
    if force and partial_path.exists():
        log.info("--force: 清空 partial 文件 %s", partial_path)
        partial_path.unlink()
    done = {} if force else _load_partial(res_dir)
    if done:
        log.info("发现 %d 条 partial 打分,断点续跑（跳过已完成 story）", len(done))

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    results_map: dict[int, dict[str, Any]] = {}
    errors_map: dict[int, str] = {}
    skipped = 0
    partial_lock = threading.Lock()

    def _score_one(i: int, mf: dict) -> tuple[str, int, Any]:
        key = mf.get("tapd_id") or mf.get("story_key") or "?"
        if key in done:
            return ("skip", i, done[key])
        log.info("[%d/%d] 打分 %s", i, len(manifests), key)
        try:
            rec = score_manifest(ds_dir, mf, with_diffs=with_diffs)
            return ("ok", i, rec)
        except Exception as e:  # noqa: BLE001
            log.exception("打分失败 %s", key)
            return ("err", i, f"{key}: {e}")

    if concurrency == 1:
        for i, mf in enumerate(manifests, 1):
            kind, idx, val = _score_one(i, mf)
            if kind == "skip":
                skipped += 1
                results_map[idx] = val
            elif kind == "ok":
                results_map[idx] = val
                _append_partial(res_dir, val)
            else:
                errors_map[idx] = val
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(_score_one, i, mf): i for i, mf in enumerate(manifests, 1)}
            for future in concurrent.futures.as_completed(futures):
                kind, idx, val = future.result()
                if kind == "skip":
                    skipped += 1
                    results_map[idx] = val
                elif kind == "ok":
                    results_map[idx] = val
                    with partial_lock:
                        _append_partial(res_dir, val)
                else:
                    errors_map[idx] = val

    for idx in sorted(results_map):
        results.append(results_map[idx])
    for idx in sorted(errors_map):
        errors.append(errors_map[idx])
    if skipped:
        log.info("本次续跑跳过 %d 条,新增打分 %d 条", skipped, len(results) - skipped)

    consistency = _self_consistency(ds_dir, manifests, n=10, seed=seed, with_diffs=False)

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


def _self_consistency(ds_dir: Path, manifests: list[dict], n: int = 10, seed: int = 42, with_diffs: bool = True) -> dict[str, Any]:
    """随机 n 个实体的 spec/plan 各评 2 次,统计维度分差 ≤1 的比例。"""
    rng = random.Random(seed)
    sample = rng.sample(manifests, min(n, len(manifests)))
    pairs: list[dict[str, Any]] = []
    diffs: list[int] = []
    for mf in sample:
        key = mf.get("tapd_id") or mf.get("story_key") or "?"
        for doc_key in ("spec", "plan"):
            text = _read_artifact(ds_dir, mf, doc_key)
            if not text:
                continue
            try:
                if doc_key == "spec":
                    prd = _read_artifact(ds_dir, mf, "prd")
                    a = judges.judge_spec(prd, text, "")
                    b = judges.judge_spec(prd, text, "")
                else:
                    spec = _read_artifact(ds_dir, mf, "spec")
                    a = judges.judge_plan(text, spec)
                    b = judges.judge_plan(text, spec)
            except Exception as e:  # noqa: BLE001
                log.warning("自洽性抽查失败 %s/%s: %s", key, doc_key, e)
                continue
            for dim, va in a.model_dump().items():
                if isinstance(va, int):
                    vb = b.model_dump().get(dim, va)
                    d = abs(int(va) - int(vb))
                    diffs.append(d)
                    pairs.append({"story_key": key, "doc": doc_key, "dim": dim, "diff": d})
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


def _all_dims(spec: dict | None, plan: dict | None, delivery: dict | None, conf: dict | None) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for d in ("completeness", "template_compliance", "acceptability"):
        if spec and d in spec:
            out.append((f"spec.{d}", spec[d]))
    for d in ("specificity", "spec_alignment", "verifiability"):
        if plan and d in plan:
            out.append((f"plan.{d}", plan[d]))
    for d in ("message_quality", "granularity", "rework"):
        if delivery and d in delivery:
            out.append((f"delivery.{d}", delivery[d]))
    for d in ("alignment", "coverage", "scope_drift"):
        if conf and d in conf:
            out.append((f"conf.{d}", conf[d]))
    return out


def _render_md(payload: dict, date: str) -> str:
    results = payload["stories"]
    con = payload["consistency"]
    lines = [
        f"# Baseline {date}",
        "",
        f"- 模型: `{payload['model']}`",
        f"- 评分实体数: {payload['count']}",
        f"- 失败: {len(payload['errors'])}",
        "",
        "## 总体分布",
        "",
        "| 维度 | 均分 | 中位 | 最小 | 最大 |",
        "|------|------|------|------|------|",
    ]
    by_dim: dict[str, list[int]] = {}
    for r in results:
        for dim, score in _all_dims(
            r.get("spec_score"), r.get("plan_score"), r.get("delivery_score"), r.get("conformance_score")
        ):
            by_dim.setdefault(dim, []).append(score)
    hist: dict[int, int] = {}
    for dim, vals in sorted(by_dim.items()):
        for v in vals:
            hist[v] = hist.get(v, 0) + 1
        lines.append(
            f"| {dim} | {mean(vals):.2f} | {sorted(vals)[len(vals)//2]} | {min(vals)} | {max(vals)} |"
        )
    lines += ["", "| 分数 | 出现次数 |", "|------|----------|"]
    for s in sorted(hist):
        lines.append(f"| {s} | {hist[s]} |")

    lines += ["", "## 低分 case Top 10（各维度合计）", ""]
    ranked = sorted(
        results,
        key=lambda r: sum(s for _, s in _all_dims(
            r.get("spec_score"), r.get("plan_score"), r.get("delivery_score"), r.get("conformance_score")
        )),
    )
    for r in ranked[:10]:
        ss, ps, dv, cf = r.get("spec_score"), r.get("plan_score"), r.get("delivery_score"), r.get("conformance_score")
        total = sum(s for _, s in _all_dims(ss, ps, dv, cf))
        title = (r.get("title") or "")[:40]
        key = r.get("tapd_id") or r.get("story_key") or "?"
        lines.append(f"### {key} — {title}（合计 {total}）")
        if ss:
            lines.append(
                f"- spec: completeness={ss.get('completeness')}, template={ss.get('template_compliance')}, "
                f"acceptability={ss.get('acceptability')}"
            )
        if dv:
            lines.append(
                f"- delivery: msg={dv.get('message_quality')}, gran={dv.get('granularity')}, rework={dv.get('rework')}"
            )
        if cf:
            lines.append(
                f"- conf: alignment={cf.get('alignment')}, coverage={cf.get('coverage')}, "
                f"scope_drift={cf.get('scope_drift')}（参照:{cf.get('reference_type')}）"
            )
        for f in ((ss or {}).get("findings") or [])[:2] + ((cf or {}).get("findings") or [])[:2]:
            lines.append(f"  - {f}")

    lines += ["", "## spec-代码漂移 case 列表", ""]
    drift = [
        (r.get("tapd_id") or r.get("story_key") or "?", r.get("conformance_score"))
        for r in results
        if r.get("conformance_score") and r["conformance_score"].get("alignment", 0) <= 2
    ]
    if drift:
        for key, cf in drift:
            lines.append(f"- **{key}**: alignment={cf.get('alignment')} coverage={cf.get('coverage')} scope_drift={cf.get('scope_drift')} — {cf.get('summary','')[:80]}")
    else:
        lines.append("（无 alignment ≤2 的严重漂移 case）")

    lines += [
        "",
        "## 自洽性",
        "",
        f"- 抽查对: {con['n_pairs']}（{con['n_stories']} 个实体 spec/plan 各 2 次）",
        f"- 维度分差 ≤1 比例: **{con['diff_le_1_ratio']:.1%}**（要求 >80%）",
        f"- 平均分差: {con['mean_diff']}",
        f"- 达标: {'✅' if con['pass_rate_80'] else '❌'}",
        "",
    ]
    if payload["errors"]:
        lines += ["## 失败列表", ""]
        lines += [f"- {e}" for e in payload["errors"]]
    return "\n".join(lines) + "\n"
