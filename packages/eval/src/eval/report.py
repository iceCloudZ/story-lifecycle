"""回归报告 — 回放 artifacts 过 judges,双重对比 gold 基线分 / 上次回放分。

产出 ``results/regression_<YYYYMMDD>.md``:每 story 各维度分（gold / 本次 /
delta）,任一维度跌 >1 标 🔴 并附 judge findings;回放失败 story 单列不计回归。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import dataset
from . import judges
from .baseline import RESULTS_DIR, _read_artifact

log = logging.getLogger("eval.report")


def _latest_baseline(results_dir: Path) -> dict | None:
    files = sorted(results_dir.glob("baseline_*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _latest_replay_dir(results_dir: Path) -> Path | None:
    dirs = sorted(results_dir.glob("replay_*"))
    return dirs[-1] if dirs else None


def _baseline_score(baseline: dict | None, story_key: str) -> dict | None:
    if not baseline:
        return None
    for s in baseline.get("stories", []):
        if s["story_key"] == story_key:
            return s
    return None


def _score_replay_story(ds_dir: Path, mf: dict, texts: dict[str, str]) -> dict[str, Any]:
    """对回放产出打 SpecScore/PlanScore/ConformanceScore。"""
    prd = _read_artifact(ds_dir, mf, "prd")
    spec = texts.get("story/spec.md", "")
    plan = texts.get("story/plan.md", "")
    template = ""
    tmpl = Path(os.environ.get("EVAL_SPEC_TEMPLATE") or "D:/hc-all/docs/spec-template.md")
    if tmpl.exists():
        template = dataset._read_text_robust(tmpl)
    build_artifacts = {k: v for k, v in texts.items() if k.startswith(("git_", "story/test"))}
    result: dict[str, Any] = {}
    if spec:
        result["spec_score"] = judges.judge_spec(prd, spec, template).model_dump()
    if plan:
        result["plan_score"] = judges.judge_plan(plan, spec if spec else "（回放未产出 spec）").model_dump()
    if spec and build_artifacts:
        result["conformance_score"] = judges.judge_conformance(spec, build_artifacts).model_dump()
    return result


def _dims_spec(score: dict) -> list[tuple[str, int]]:
    out = []
    for d in ("completeness", "template_compliance", "acceptability"):
        if d in score:
            out.append((d, score[d]))
    return out


def _dims_plan(score: dict) -> list[tuple[str, int]]:
    out = []
    for d in ("specificity", "spec_alignment", "verifiability"):
        if d in score:
            out.append((d, score[d]))
    return out


def _dims_conf(score: dict) -> list[tuple[str, int]]:
    out = []
    for d in ("alignment", "coverage"):
        if d in score:
            out.append((d, score[d]))
    return out


def run_report(
    dataset_dir: str | Path | None = None,
    results_dir: str | Path | None = None,
) -> dict[str, Any]:
    """评分本次回放并生成回归报告。"""
    from .judges import configure_llm_env

    configure_llm_env()
    ds_dir = Path(dataset_dir) if dataset_dir else Path(__file__).resolve().parent.parent.parent / "dataset"
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR

    baseline = _latest_baseline(res_dir)
    replay_dir = _latest_replay_dir(res_dir)
    if not replay_dir:
        raise RuntimeError("没有 replay_* 目录——先跑 `eval replay`")
    summary_path = replay_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}

    manifests = {m["story_key"]: m for m in dataset.load_manifests(ds_dir)}
    rows: list[dict[str, Any]] = []
    for entry in summary.get("stories", []):
        key = entry["story_key"]
        row = {"story_key": key, "status": entry.get("status"), "error": entry.get("error")}
        if entry.get("status") != "ok":
            rows.append(row)
            continue
        story_dir = replay_dir / dataset._safe_segment(key)
        texts: dict[str, str] = {}
        for p in story_dir.iterdir():
            if p.is_file() and p.suffix in {".md", ".json", ".txt"}:
                texts[p.name] = dataset._read_text_robust(p)
        # 键名对齐 _score_replay_story 期待的形式
        mapped = {}
        for name in ("spec.md", "plan.md", "test-report.md"):
            if name in texts:
                mapped[f"story/{name}"] = texts[name]
        for name in ("git_status", "git_diff_stat"):
            if name in texts:
                mapped[name] = texts[name]
        try:
            score = _score_replay_story(ds_dir, manifests.get(key) or {}, mapped)
        except Exception as e:  # noqa: BLE001
            row["error"] = f"评分失败: {e}"
            log.exception("评分 %s 失败", key)
            rows.append(row)
            continue
        gold = _baseline_score(baseline, key) or {}
        gold_spec = (gold.get("spec_score") or {}) if gold else {}
        gold_plan = (gold.get("plan_score") or {}) if gold else {}
        now_spec = score.get("spec_score") or {}
        now_plan = score.get("plan_score") or {}
        row["gold"] = {"spec": gold_spec, "plan": gold_plan}
        row["now"] = score
        dims = []
        for d in ("completeness", "template_compliance", "acceptability"):
            g, n = gold_spec.get(d), now_spec.get(d)
            if g is not None and n is not None:
                dims.append({"dim": f"spec.{d}", "gold": g, "now": n, "delta": n - g})
        for d in ("specificity", "spec_alignment", "verifiability"):
            g, n = gold_plan.get(d), now_plan.get(d)
            if g is not None and n is not None:
                dims.append({"dim": f"plan.{d}", "gold": g, "now": n, "delta": n - g})
        for d in ("alignment", "coverage"):
            n = (score.get("conformance_score") or {}).get(d)
            if n is not None:
                dims.append({"dim": f"conf.{d}", "gold": None, "now": n, "delta": None})
        row["dims"] = dims
        rows.append(row)

    date = _dt.date.today().strftime("%Y%m%d")
    md = _render_md(rows, date, baseline, replay_dir)
    md_path = res_dir / f"regression_{date}.md"
    md_path.write_text(md, encoding="utf-8")
    log.info("回归报告: %s", md_path)
    return {"md": str(md_path), "rows": rows}


def _render_md(rows: list[dict], date: str, baseline: dict | None, replay_dir: Path) -> str:
    lines = [
        f"# 回放回归报告 {date}",
        "",
        f"- 回放目录: `{replay_dir.name}`",
        f"- 对照 baseline: `{Path(baseline and baseline.get('generated_at') or '').name or '（无）'}`"
        if baseline
        else f"- 对照 baseline: 无（无法算 delta）",
        f"- 模型: `{baseline.get('model') if baseline else '?'}`",
        "",
        "## 回归对比",
        "",
        "| story | 维度 | gold | 本次 | delta |",
        "|-------|------|------|------|-------|",
    ]
    regressions = []
    for row in rows:
        key = row["story_key"]
        if row.get("status") != "ok":
            continue
        for d in row.get("dims", []):
            if d["gold"] is None:
                lines.append(f"| {key} | {d['dim']} | - | {d['now']} | 新增 |")
            else:
                mark = f" {'🔴' if d['delta'] <= -1 else ''}" if d["delta"] < 0 else ""
                lines.append(f"| {key} | {d['dim']} | {d['gold']} | {d['now']} | {d['delta']:+d}{mark} |")
                if d["delta"] <= -1:
                    regressions.append((key, d["dim"], d))
    lines += ["", "## 执行失败（不计回归）", ""]
    failed = [r for r in rows if r.get("status") != "ok"]
    if failed:
        for r in failed:
            lines.append(f"- **{r['story_key']}**: {r.get('error') or r.get('status')}")
    else:
        lines.append("（无）")
    if regressions:
        lines += ["", "## 🔴 回归详情", ""]
        for key, dim, d in regressions:
            lines.append(f"### {key} — {dim}（gold {d['gold']} → 本次 {d['now']}）")
            now_score = next(r for r in rows if r["story_key"] == key)["now"]
            prefix = dim.split(".")[0]
            score = now_score.get("spec_score") or now_score.get("plan_score") or now_score.get("conformance_score") or {}
            for f in (score.get("findings") or [])[:4]:
                lines.append(f"- {f}")
    lines.append("")
    return "\n".join(lines)
