"""快照 v2 选样（§2 配额）+ held-out 密封（§3）。

输入:
- v2 merge_scores.jsonl（Go 重基，新标签）— 类别标签来源
- v1 snapshot stories_matched.jsonl / tapd_stories.jsonl（关联/参照物分层）
- results/failure_patterns_20260805.md（21 主题，§2.1 配额）
- sandbox/gold/samples20.json（v1 A/B/C/D，去重用）
- results/pipeline_b_injected_20260805.json（B 注入 5 条，去重）
- results/gate_replay_refs_20260805/（167 条回测，去重）

输出:
- snapshot_v2_20260806/replay_samples.jsonl（~50 条）
- snapshot_v2_20260806/held_out.jsonl（15 条，种子 42 密封）
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_V1 = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
SNAP_V2 = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806"
RESULTS = PACKAGE_ROOT / "results"
SANDBOX = PACKAGE_ROOT / "sandbox"


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def load_v2_rows() -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    p = SNAP_V2 / "merge_scores.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[(r["repo"], r["merge_hash"])] = r  # 后写覆盖（含 backfill 追加行）
    return list(rows.values())


def load_matched() -> list[dict]:
    out: list[dict] = []
    p = SNAP_V1 / "stories_matched.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def load_tapd() -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = SNAP_V1 / "tapd_stories.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["tapd_id"]] = rec
    return out


def parse_failure_topics() -> list[dict]:
    """解析 failure_patterns_20260805.md → [{topic, count, cases: [{repo, hash10, tapd, reason}]}]。"""
    p = RESULTS / "failure_patterns_20260805.md"
    topics: list[dict] = []
    cur = None
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"### (.+?)（(\d+) 个 merge）", line)
        if m:
            cur = {"topic": m.group(1), "count": int(m.group(2)), "cases": []}
            topics.append(cur)
            continue
        m = re.match(r"\s*- (\S+):([0-9a-f]{10,}) tapd=(\d*) align=\d+ \| (.+)", line)
        if m and cur is not None:
            cur["cases"].append({
                "repo": m.group(1), "hash10": m.group(2), "tapd": m.group(3), "reason": m.group(4),
            })
    return topics


def load_samples20() -> list[dict]:
    p = SANDBOX / "gold" / "samples20.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def load_b_inject() -> list[dict]:
    p = RESULTS / "pipeline_b_injected_20260805.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def load_gate_replay_keys() -> set[tuple[str, str]]:
    """gate 回测 167 条 → (repo, hash10)。"""
    keys: set[tuple[str, str]] = set()
    for f in (RESULTS / "gate_replay_refs_20260805").glob("*.md"):
        m = re.match(r"(pos|neg)_([^_]+)_([0-9a-f]{10,})_", f.name)
        if m:
            keys.add((m.group(2), m.group(3)))
    return keys


def merged_tapd(row: dict) -> str:
    return row.get("tapd_id") or ""


def is_pipeline(row: dict) -> bool:
    return bool(row.get("story_key"))


# ---------------------------------------------------------------------------
# §2 配额选样
# ---------------------------------------------------------------------------


def select_topic_samples(v2_rows: list[dict]) -> list[dict]:
    """§2.1 失败主题配额：21 主题 × 1 代表 case。

    代表 case = 该主题典型 case 中优先管线内（有 story_key），否则第一条。
    """
    topics = parse_failure_topics()
    by_key = {(r["repo"], r["merge_hash"][:10]): r for r in v2_rows}
    samples: list[dict] = []
    for t in topics:
        picked = None
        for c in t["cases"]:
            row = by_key.get((c["repo"], c["hash10"]))
            if row is None:
                continue
            if is_pipeline(row):
                picked = (c, row)
                break
        if picked is None:
            for c in t["cases"]:
                row = by_key.get((c["repo"], c["hash10"]))
                if row is not None:
                    picked = (c, row)
                    break
        if picked is None:
            samples.append({
                "category": "topic", "topic": t["topic"], "tapd_id": t["cases"][0].get("tapd", ""),
                "repo": t["cases"][0]["repo"], "merge": t["cases"][0]["hash10"],
                "reason": "主题代表 case（merge 不在 v2 评分集）", "expected": "gate 应拦且 finding 命中主题",
            })
            continue
        c, row = picked
        should_block = classify_v2(row) == "B"
        samples.append({
            "category": "topic",
            "topic": t["topic"],
            "tapd_id": merged_tapd(row),
            "story_key": row.get("story_key", ""),
            "repo": row["repo"],
            "merge": row["merge_hash"],
            "merge10": row["merge_hash"][:10],
            "v2_align": (row.get("conformance_score") or {}).get("alignment"),
            "v2_cov": (row.get("conformance_score") or {}).get("coverage"),
            "reason": f"失败主题「{t['topic']}」代表 case（{c['reason'][:60]}）",
            "expected": ("gate 应拦且 finding 命中主题" if should_block
                         else "gate 放行（v2 重评已合格，标签迁移——v1 主题背景保留）"),
        })
    return samples


def classify_v2(row: dict) -> str:
    """按 v2 新标签分类（迭代 1 收尾口径）：A=align≥4 且 cov≥4；B=align≤2 或 cov≤2。"""
    cf = row.get("conformance_score") or {}
    if cf.get("alignment") is None:
        return ""
    if cf.get("alignment", 5) >= 4 and cf.get("coverage", 5) >= 4:
        return "A"
    if cf.get("alignment", 5) <= 2 or cf.get("coverage", 5) <= 2:
        return "B"
    return "mid"


def entity_ref_type(ent: dict, tapd: dict) -> str:
    """参照物类型：spec/prd/story_refs/tapd/无评分。"""
    ed = ent.get("evidence_dir") or ""
    if ed:
        d = Path(ed)
        for doc_key, cands in (("spec", ["spec.md", "Spec.md", "design.md"]),
                               ("prd", ["PRD.md", "prd.md", "Prd.md"])):
            for cand in cands:
                f = d / cand
                if f.exists() and f.stat().st_size > 0:
                    return doc_key
    tid = ent.get("tapd_id") or ""
    rec = tapd.get(tid) or {}
    desc = rec.get("description") or ""
    import sys

    sys.path.insert(0, str(PACKAGE_ROOT / "src"))
    from eval.ref_fetch import is_link_only

    if is_link_only(desc):
        p = SNAP_V1 / "story_refs" / f"{tid}.md"
        if p.exists() and len(p.read_text(encoding="utf-8", errors="replace").strip()) >= 100:
            return "story_refs"
    text = re.sub(r"<[^>]+>", " ", desc or "")
    text = re.sub(r"\s+", " ", text).strip()
    return "tapd" if text else "no_score"


def is_cross_service(ent: dict) -> bool:
    repos = {dl["repo"] for dl in ent.get("deliveries", [])}
    return len(repos) >= 2


def select_abcd_refresh(v2_rows: list[dict], matched: list[dict], tapd: dict, exclude_keys: set[tuple[str, str]] | None = None) -> list[dict]:
    """§2.3 A/B/C/D 刷新（~15 条，与 v1 样本去重 + 与 topic 样本去重）。"""
    samples20 = load_samples20()
    b_inject = load_b_inject()
    exclude_tapd = {s["tapd_id"] for s in samples20} | {s["tapd_id"] for s in b_inject}
    exclude_keys = exclude_keys or set()

    ent_by_tid = {e.get("tapd_id"): e for e in matched if e.get("tapd_id")}
    # 每个 merge 行找所属实体（参照物/跨服务信息）
    row_info: list[tuple[dict, dict]] = []
    for r in v2_rows:
        tid = merged_tapd(r)
        ent = ent_by_tid.get(tid)
        if ent is None:
            continue
        row_info.append((r, ent))

    samples = []
    picked_keys: set[tuple[str, str]] = set()

    def _pick(cls: str, want: int) -> list[dict]:
        out = []
        for r, ent in row_info:
            if merged_tapd(r) in exclude_tapd:
                continue
            key = (r["repo"], r["merge_hash"][:10])
            if key in exclude_keys or key in picked_keys:
                continue
            cls_now = classify_v2(r)
            ref = entity_ref_type(ent, tapd)
            if cls == "A" and cls_now == "A":
                pass
            elif cls == "B" and cls_now == "B":
                pass
            elif cls == "C" and ref == "story_refs":
                pass
            elif cls == "D" and is_cross_service(ent):
                pass
            else:
                continue
            picked_keys.add(key)
            out.append({
                "category": f"abcd-{cls}",
                "cls": cls,
                "tapd_id": merged_tapd(r),
                "story_key": r.get("story_key", ""),
                "repo": r["repo"], "merge": r["merge_hash"],
                "v2_align": (r.get("conformance_score") or {}).get("alignment"),
                "v2_cov": (r.get("conformance_score") or {}).get("coverage"),
                "ref_type": ref,
                "cross_service": is_cross_service(ent),
                "reason": f"{cls} 类刷新（v2 新标签）",
                "expected": {"A": "gate 放行", "B": "gate 应拦", "C": "story_refs 参照物验证", "D": "跨服务验证"}[cls],
            })
            if len(out) >= want:
                break
        return out

    for cls in ("A", "B", "C", "D"):
        samples.extend(_pick(cls, 4))
    return samples


def select_gap_fill(matched: list[dict], tapd: dict, v2_rows: list[dict]) -> list[dict]:
    """§2.4 场景空格补样：无评分×单仓 6、跨服务×无评分 2。"""
    row_keys = {(r["repo"], r["merge_hash"]) for r in v2_rows}
    single_repo_no_score, cross_no_score = [], []
    for ent in matched:
        if not ent.get("deliveries"):
            continue
        # 实体级「无评分」：参照物无
        ref = entity_ref_type(ent, tapd)
        if ref != "no_score":
            continue
        cross = is_cross_service(ent)
        if not cross:
            single_repo_no_score.append(ent)
        else:
            cross_no_score.append(ent)
    rng = random.Random(42)
    out = []

    def _mk(ent: dict, label: str, reason: str) -> dict:
        dl = ent["deliveries"][0]
        return {
            "category": "gap", "cls": label,
            "tapd_id": ent.get("tapd_id", ""), "story_key": ent.get("story_key", ""),
            "repo": dl["repo"], "merge": dl["merge_hash"],
            "reason": reason,
            "expected": "gate 行为记录（无评分基线补充）",
        }

    for ent in rng.sample(single_repo_no_score, min(6, len(single_repo_no_score))):
        out.append(_mk(ent, "gap-single-no-score",
                       f"空格补样：无评分×单仓（池 {len(single_repo_no_score)}，种子42 抽 6）"))
    for ent in rng.sample(cross_no_score, min(2, len(cross_no_score))):
        out.append(_mk(ent, "gap-cross-no-score",
                       f"空格补样：跨服务×无评分（池 {len(cross_no_score)}，种子42 抽 2）"))
    return out


# ---------------------------------------------------------------------------
# §3 held-out
# ---------------------------------------------------------------------------


def select_held_out(v2_rows: list[dict], matched: list[dict], tapd: dict) -> list[dict]:
    """§3 held-out 15 条：v2 有关联 merge，种子 42。

    排除: human_confirmed/human_recalibrated 链接、samples20、b_inject、
    gate 回测 167 条、replay_samples 全部键（topic/abcd/gap/construct）。
    """
    samples20 = load_samples20()
    b_inject = load_b_inject()
    gate_keys = load_gate_replay_keys()
    exclude_tapd = {s["tapd_id"] for s in samples20} | {s["tapd_id"] for s in b_inject}
    sample_keys = set()
    for line in (SNAP_V2 / "replay_samples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            s = json.loads(line)
            if s.get("merge"):
                sample_keys.add((s.get("repo", ""), s["merge"][:10]))
            elif s.get("merge10"):
                sample_keys.add((s.get("repo", ""), s["merge10"]))

    # v2 有关联（tapd_id 非空）且未进任何验收样本集
    pool = []
    for r in v2_rows:
        if not merged_tapd(r):
            continue
        if merged_tapd(r) in exclude_tapd:
            continue
        if (r["repo"], r["merge_hash"][:10]) in gate_keys:
            continue
        if (r["repo"], r["merge_hash"][:10]) in sample_keys:
            continue
        ent = next((e for e in matched if e.get("tapd_id") == merged_tapd(r)), None)
        if ent is None:
            continue
        # 排除人工确认/再裁决的链接
        human = False
        for dl in ent.get("deliveries", []):
            if dl.get("human_confirmed") or dl.get("human_recalibrated") or ent.get("human_recalibrated"):
                human = True
        if human:
            continue
        pool.append(r)
    rng = random.Random(42)
    picked = rng.sample(pool, min(15, len(pool)))
    out = []
    for r in picked:
        cf = r.get("conformance_score") or {}
        out.append({
            "tapd_id": merged_tapd(r),
            "story_key": r.get("story_key", ""),
            "repo": r["repo"], "merge": r["merge_hash"],
            "v2_align": cf.get("alignment"), "v2_cov": cf.get("coverage"),
            "sealed": True,
            "note": "密封：仅阶段验收用，迭代期间禁止用于调优",
        })
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> dict:
    v2_rows = load_v2_rows()
    matched = load_matched()
    tapd = load_tapd()

    topic = select_topic_samples(v2_rows)
    topic_keys = {(s.get("repo", ""), (s.get("merge") or s.get("merge10") or "")[:10]) for s in topic}
    abcd = select_abcd_refresh(v2_rows, matched, tapd, exclude_keys=topic_keys)
    gap = select_gap_fill(matched, tapd, v2_rows)

    samples = topic + abcd + gap
    print(f"选样: topic {len(topic)} / abcd {len(abcd)} / gap {len(gap)} → 共 {len(samples)}")

    # 构造样本（§2.2）6 条由 v2_construct.py 生成，这里预留清单
    construct = json.loads((SNAP_V2 / "construct_samples.json").read_text(encoding="utf-8")) \
        if (SNAP_V2 / "construct_samples.json").exists() else []
    samples = samples + construct
    print(f"含构造样本 {len(construct)} 条 → 共 {len(samples)}")

    SNAP_V2.mkdir(parents=True, exist_ok=True)
    with open(SNAP_V2 / "replay_samples.jsonl", "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    held = select_held_out(v2_rows, matched, tapd)
    with open(SNAP_V2 / "held_out.jsonl", "w", encoding="utf-8") as f:
        for s in held:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"held-out: {len(held)} 条（池 {len([r for r in v2_rows if r.get('tapd_id')])} 有关联，排除后池 {len([r for r in v2_rows if r.get('tapd_id') and (r['repo'], r['merge_hash'][:10]) not in load_gate_replay_keys()])}）")

    return {"samples": len(samples), "held_out": len(held)}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
