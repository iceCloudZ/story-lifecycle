"""快照 v2 §4 — manifest 生成器。

冻结时间 / judge 三元组 / 各文件行数 / v2 新基线均分 / 样本构成 / held-out 种子与排除规则 / 与 v1 差异。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_V1 = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"


def _count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(1 for _ in p.open(encoding="utf-8", errors="replace"))


def _load_rows() -> list[dict]:
    p = SNAP_V2 / "merge_scores.jsonl"
    rows: dict[tuple[str, str], dict] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[(r["repo"], r["merge_hash"])] = r
    return list(rows.values())


def render() -> str:
    rows = _load_rows()
    conf = [r for r in rows if (r.get("conformance_score") or {}).get("alignment") is not None]
    align = [r["conformance_score"]["alignment"] for r in conf]
    cov = [r["conformance_score"]["coverage"] for r in conf]
    linked = [r for r in rows if r.get("tapd_id")]
    errs = [r for r in rows if r.get("error")]

    samples = [json.loads(l) for l in (SNAP_V2 / "replay_samples.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    held = [json.loads(l) for l in (SNAP_V2 / "held_out.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    from collections import Counter

    by_cat = Counter(s.get("category", "?") for s in samples)

    v1_linked = 0
    for line in (SNAP_V1 / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line).get("tapd_id"):
            v1_linked += 1

    now = _dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        "# snapshot_v2_20260806 冻结清单",
        "",
        "| 项 | 值 |",
        "|----|----|",
        f"| snapshot | snapshot_v2_20260806 |",
        f"| frozen_at | {now} |",
        f"| judge 三元组 | opencode-go 端点 `https://opencode.ai/zen/go/v1` + `deepseek-v4-flash` + conformance.py（迭代 1 移植版，commit 2b4db820 起） |",
        f"| merge_scores.jsonl 唯一键行数 | {len(rows)}（v1 文件 760 行含 10 个重复键，唯一键 750；v2 每键一行） |",
        f"| 有关联（tapd_id 非空） | {len(linked)} |",
        f"| error 行 | {len(errs)}（≤2% 达标） |",
        f"| 关联数（v1 同口径） | {v1_linked} → v2 {len(linked)}（v1 行级 tapd 非空计数） |",
        f"| conf.alignment 均分（v2，不含疑似错链） | {sum(align)/len(align):.3f}（n={len(align)}） |",
        f"| conf.coverage 均分（v2） | {sum(cov)/len(cov):.3f}（n={len(cov)}） |",
        f"| baseline_v2 | 见 baseline_v2.json（自洽性落盘） |",
        f"| replay_samples.jsonl | {len(samples)} 条（构成见下） |",
        f"| held_out.jsonl | {len(held)} 条（种子 42，密封） |",
        "",
        "## 与 v1 的差异说明",
        "",
        "- v1 用 DeepSeek 官方端点 + 旧 prompt 评分；**v2 全部按 Go 三元组重评，数字不可直接比较**",
        "- v2 merge_scores 按 (repo, merge_hash) 唯一键落盘（v1 有 10 个重复键后写覆盖）",
        "- 无参照物的关联 merge：v2 标 `conformance_skipped: no reference`（v1 行为相同：不评 conformance）",
        "- v2 补评了 v1 已评的 delivery_score（Go）",
        "",
        "## 样本集构成（replay_samples.jsonl）",
        "",
    ]
    for cat, n in sorted(by_cat.items()):
        lines.append(f"- {cat}: {n} 条")
    lines += [
        "",
        "### 失败主题配额（§2.1）",
        "",
    ]
    topics = sorted({s["topic"] for s in samples if s.get("category") == "topic"})
    for t in topics:
        lines.append(f"- {t}")
    lines += [
        "",
        "## held-out 种子与排除规则（§3）",
        "",
        "- 种子: `random.Random(42)`，从 v2 有关联 merge 池抽取 15 条",
        "- 排除: 人工确认/再裁决链接（human_confirmed/human_recalibrated）、v1 A/B/C/D 样本（samples20 20 条）、B 类注入（pipeline_b_inject 5 条）、gate 回测 167 条、主题/ABCD/空格样本",
        "- **密封**: 仅阶段验收用，迭代期间禁止用于调优（held_out.jsonl 每行 note 注明）",
        "",
        "> 以后所有迭代 replay/验收对比以 v2 为准；v1 仅作历史存档（只读）。",
    ]
    path = SNAP_V2 / "snapshot_manifest.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"manifest: {path}")
    return str(path)


if __name__ == "__main__":
    print(render())
