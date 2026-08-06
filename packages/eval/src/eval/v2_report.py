"""快照 v2 §1.3 — full_scan_mine_v2 报告（基于 v2 merge_scores 渲染）。

头部注明「v2 = Go 三元组重基，与 v1 数字不可直接比较」；
主体: conf.alignment 基线均分 / 管线内 vs 管线外 / 按 repo / 按月份。
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from statistics import mean

PACKAGE_ROOT = Path(r"D:\github\story-lifecycle\packages\eval")
SNAP_V1 = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"
RESULTS = PACKAGE_ROOT / "results"

sys.path.insert(0, str(PACKAGE_ROOT / "src"))
sys.path.insert(0, r"D:\github\story-lifecycle\packages\story-lifecycle\src")

from eval.scanall import _month_key, _is_suspected_wrong_link, _drift_severity  # noqa: E402
from eval import judges  # noqa: E402


def _conf_rows(rows: list[dict]) -> list[dict]:
    return [
        r for r in rows
        if (r.get("conformance_score") or {}).get("alignment") is not None
        and not _is_suspected_wrong_link(r)
    ]


def _avg(rs: list[dict], dim: str) -> str:
    vals = [
        (r.get("conformance_score") or {}).get(dim)
        for r in rs if isinstance((r.get("conformance_score") or {}).get(dim), int)
    ]
    return f"{mean(vals):.2f}" if vals else "-"


def render() -> str:
    rows = [json.loads(l) for l in (SNAP_V2 / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    date = _dt.date.today().strftime("%Y%m%d")
    conf_rows = _conf_rows(rows)
    n_linked = sum(1 for r in rows if r.get("tapd_id"))
    n_err = sum(1 for r in rows if r.get("error"))
    suspected = [r for r in rows if _is_suspected_wrong_link(r)]

    lines = [
        f"# 个人扫描报告（v2 重基） {date}",
        "",
        "> **v2 = Go 三元组重基**（opencode-go 端点 + deepseek-v4-flash + 迭代 1 prompt），",
        "> 与 v1（DeepSeek 官方端点 + 旧 prompt）**数字不可直接比较**，跨版本对比需标注端点漂移。",
        "",
        f"- 扫描行数: {len(rows)}（与 v1 同键集合重评）",
        f"- 有关联 story: {n_linked} / 失败: {n_err}",
        f"- 疑似错链（alignment=1 且完全无关）: {len(suspected)}（不计入均分）",
        f"- LLM 端点: opencode-go / 模型: deepseek-v4-flash / prompt: conformance.py（迭代 1 移植版）",
        "",
        "## 总体分布（v2）",
        "",
        "| 维度 | 均分 | 中位 | 最小 | 最大 |",
        "|------|------|------|------|------|",
    ]
    by_dim: dict[str, list[int]] = {}
    for r in conf_rows:
        cf = r["conformance_score"]
        for d in ("alignment", "coverage", "scope_drift"):
            by_dim.setdefault(f"conf.{d}", []).append(cf[d])
    for d in ("message_quality", "granularity", "rework"):
        vals = [r["delivery_score"][d] for r in rows if (r.get("delivery_score") or {}).get(d) is not None]
        if vals:
            by_dim[f"delivery.{d}"] = vals
    for dim, vals in sorted(by_dim.items()):
        lines.append(f"| {dim} | {mean(vals):.2f} | {sorted(vals)[len(vals)//2]} | {min(vals)} | {max(vals)} |")

    lines += ["", "## 管线内 vs 管线外 ★", ""]
    in_pipe = [r for r in conf_rows if r.get("story_key")]
    out_pipe = [r for r in conf_rows if not r.get("story_key")]
    lines.append(f"- 管线内（有 story_key）: {len(in_pipe)} 个 → alignment {_avg(in_pipe, 'alignment')} / coverage {_avg(in_pipe, 'coverage')} / scope_drift {_avg(in_pipe, 'scope_drift')}")
    lines.append(f"- 管线外: {len(out_pipe)} 个 → alignment {_avg(out_pipe, 'alignment')} / coverage {_avg(out_pipe, 'coverage')} / scope_drift {_avg(out_pipe, 'scope_drift')}")
    a_in, a_out = _avg(in_pipe, "alignment"), _avg(out_pipe, "alignment")
    if a_in != "-" and a_out != "-":
        lines.append(f"- 对齐差距: alignment {float(a_out) - float(a_in):+.2f}（管线外 - 管线内）")

    lines += ["", "## 按 repo", ""]
    by_repo: dict[str, list[dict]] = {}
    for r in conf_rows:
        by_repo.setdefault(r["repo"], []).append(r)
    for repo, rs in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {repo}: {len(rs)} 个,conf.alignment 均分 {_avg(rs, 'alignment')}")

    lines += ["", "## 按月份", ""]
    by_month: dict[str, list[dict]] = {}
    for r in conf_rows:
        by_month.setdefault(_month_key(r.get("merged_at", "")), []).append(r)
    for month, rs in sorted(by_month.items()):
        in_n = sum(1 for r in rs if r.get("story_key"))
        lines.append(f"- {month}: {len(rs)} 个,conf.alignment 均分 {_avg(rs, 'alignment')},管线内 {in_n} 个")

    lines += ["", "## 低分 case（alignment ≤2 或 coverage ≤2，按严重度）", ""]
    drift = sorted(
        [r for r in conf_rows if (r["conformance_score"]["alignment"] <= 2 or r["conformance_score"]["coverage"] <= 2)],
        key=_drift_severity,
    )
    if drift:
        for r in drift[:30]:
            cf = r["conformance_score"]
            lines.append(f"- **{r['repo']}:{r['merge_hash'][:10]}** ({r.get('merged_at','')[:10]}) "
                         f"align={cf['alignment']} cov={cf['coverage']} drift={cf['scope_drift']} "
                         f"tapd={r.get('tapd_id','')}")
            if cf.get("summary"):
                lines.append(f"  - {cf['summary'][:110]}")
    else:
        lines.append("（无）")

    errs = [r for r in rows if r.get("error")]
    if errs:
        lines += ["", "## 失败列表", ""]
        for r in errs[:30]:
            lines.append(f"- {r['repo']}:{r['merge_hash'][:10]}: {r['error'][:120]}")

    path = RESULTS / f"full_scan_mine_v2_{date}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {path}")
    return str(path)


if __name__ == "__main__":
    print(render())
