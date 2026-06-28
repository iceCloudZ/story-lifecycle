"""结果轴一期 D：iteration_id 粗关联（weak）。

背景：精确 bug↔story（反向 ``get_related_bugs``）只覆盖 ~20% story。
TAPD bug 73% 有 iteration_id，story 100% 有 iteration_id。
本脚本用"同 iteration"撮合 bug↔story，拉宽覆盖，标 ``weak:iteration``。

输入：
  - ``scripts/out/bug_story_graph.json``（精确 bug 详情）
  - TAPD API（stories + bugs by owner）
输出：``scripts/out/bug_iteration_links.json``
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ.parent / "story-lifecycle" / "src"))

from story_lifecycle.sources.tapd_source import TapdSource  # noqa: E402


def load_precise_links(path: Path) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """从 bug_story_graph.json 加载精确 story→bug 关联和 bug 详情。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    story_bugs: dict[str, list[str]] = {}
    bug_cache: dict[str, dict] = {}
    for s in data.get("stories", []):
        sid = s["story_id"]
        bids = [b["bug_id"] for b in s.get("bugs", [])]
        if bids:
            story_bugs[sid] = bids
        for b in s.get("bugs", []):
            bug_cache[b["bug_id"]] = b
    return story_bugs, bug_cache


def fetch_stories(src: TapdSource) -> list[dict]:
    raw = src._api.get_stories(
        {"entity_type": "stories", "limit": 500, "custom_field_25": src.owner.rstrip(";")}
    ) or []
    return [r.get("Story", r) for r in raw]


def fetch_bugs(src: TapdSource) -> list[dict]:
    raw = src._api.get_bugs({"limit": 500, "current_owner": src.owner.rstrip(";")}) or []
    return [r.get("Bug", r) for r in raw]


def normalize_iter(iid: str | None) -> str:
    return (iid or "").strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", default="赵子豪")
    ap.add_argument("--workspace-id", default="44381896")
    ap.add_argument(
        "--precise",
        default=str(_PROJ / "scripts" / "out" / "bug_story_graph.json"),
    )
    ap.add_argument(
        "--out", default=str(_PROJ / "scripts" / "out" / "bug_iteration_links.json")
    )
    args = ap.parse_args()

    src = TapdSource(
        {
            "workspace_id": args.workspace_id,
            "owner": args.owner,
            "story_status": "*",
            "bug_status": "*",
        }
    )

    precise_path = Path(args.precise)
    precise_links, bug_cache = load_precise_links(precise_path)
    print(f"loaded precise links: {len(precise_links)} stories, {len(bug_cache)} bugs")

    print("fetching stories ...")
    stories = fetch_stories(src)
    print(f"  stories: {len(stories)}")

    print("fetching bugs ...")
    bugs = fetch_bugs(src)
    print(f"  bugs: {len(bugs)}")

    # iteration 元数据
    print("fetching iterations ...")
    iters_raw = src._api._call("get_iterations", {"limit": 200}) or {}
    iter_meta: dict[str, dict] = {}
    for r in iters_raw.get("data", []):
        flat = r.get("Iteration", r)
        iid = normalize_iter(flat.get("id"))
        if iid:
            iter_meta[iid] = {
                "id": iid,
                "name": flat.get("name", ""),
                "startdate": flat.get("startdate", ""),
                "enddate": flat.get("enddate", ""),
                "status": flat.get("status", ""),
            }
    print(f"  iterations: {len(iter_meta)}")

    # 建立索引
    stories_by_id: dict[str, dict] = {s["id"]: s for s in stories}
    bugs_by_id: dict[str, dict] = {b["id"]: b for b in bugs}

    stories_by_iter: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        iid = normalize_iter(s.get("iteration_id"))
        if iid and iid != "0":
            stories_by_iter[iid].append(s)

    bugs_by_iter: dict[str, list[dict]] = defaultdict(list)
    for b in bugs:
        iid = normalize_iter(b.get("iteration_id"))
        if iid and iid != "0":
            bugs_by_iter[iid].append(b)

    # 精确 link 集合
    precise_pairs: set[tuple[str, str]] = set()
    for sid, bids in precise_links.items():
        for bid in bids:
            precise_pairs.add((sid, bid))

    # 在精确集上验证 iteration 撮合语义
    same_iter_on_precise = 0
    diff_iter_on_precise = 0
    for sid, bids in precise_links.items():
        s = stories_by_id.get(sid)
        if not s:
            continue
        s_iid = normalize_iter(s.get("iteration_id"))
        if not s_iid or s_iid == "0":
            continue
        for bid in bids:
            b = bugs_by_id.get(bid)
            if not b:
                continue
            b_iid = normalize_iter(b.get("iteration_id"))
            if b_iid and b_iid == s_iid:
                same_iter_on_precise += 1
            elif b_iid:
                diff_iter_on_precise += 1

    total_precise_with_iter = same_iter_on_precise + diff_iter_on_precise
    iter_accuracy = (
        same_iter_on_precise / total_precise_with_iter
        if total_precise_with_iter
        else 0.0
    )

    # 生成 weak:iteration links
    weak_links: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for s in stories:
        sid = s["id"]
        s_iid = normalize_iter(s.get("iteration_id"))
        if not s_iid or s_iid == "0":
            continue
        for b in bugs_by_iter.get(s_iid, []):
            bid = b["id"]
            if (sid, bid) in precise_pairs:
                continue
            # 不与自己同 iteration 但不是由当前 owner 负责的 bug？
            # 这里 bugs 已经限定为 owner 的 bug，所以无需额外过滤
            weak_links[sid].append((bid, b))

    # 合并结果
    graph: list[dict] = []
    covered_stories: set[str] = set()
    total_links = 0
    weak_link_count = 0
    precise_link_count = 0

    for s in stories:
        sid = s["id"]
        s_iid = normalize_iter(s.get("iteration_id"))
        bugs_out: list[dict] = []

        # precise links
        for bid in precise_links.get(sid, []):
            b = bug_cache.get(bid) or bugs_by_id.get(bid)
            if b:
                bugs_out.append(
                    {
                        "bug_id": bid,
                        "title": b.get("title", ""),
                        "severity": b.get("severity", ""),
                        "status": b.get("status", ""),
                        "iteration_id": normalize_iter(b.get("iteration_id")),
                        "linkage": "precise",
                    }
                )
                precise_link_count += 1

        # weak links
        for bid, b in weak_links.get(sid, []):
            bugs_out.append(
                {
                    "bug_id": bid,
                    "title": b.get("title", ""),
                    "severity": b.get("severity", ""),
                    "status": b.get("status", ""),
                    "iteration_id": normalize_iter(b.get("iteration_id")),
                    "linkage": "weak:iteration",
                }
            )
            weak_link_count += 1

        if bugs_out:
            covered_stories.add(sid)

        graph.append(
            {
                "story_id": sid,
                "story_key": f"tapd-{sid}",
                "title": s.get("name", ""),
                "iteration_id": s_iid,
                "iteration_name": iter_meta.get(s_iid, {}).get("name", ""),
                "bug_count": len(bugs_out),
                "bugs": bugs_out,
            }
        )
        total_links += len(bugs_out)

    n_precise_stories = len(precise_links)
    n_weak_stories = len(
        {sid for sid in weak_links if sid not in precise_links}
    ) + len({sid for sid in weak_links if sid in precise_links})
    n_covered_total = len(covered_stories)

    # iteration 维度聚合
    iter_bug_counts: dict[str, dict] = defaultdict(
        lambda: {"name": "", "story_ids": set(), "bug_ids": set(), "n_bugs": 0}
    )
    for g in graph:
        iid = g["iteration_id"]
        if not iid or iid == "0":
            continue
        it = iter_bug_counts[iid]
        it["name"] = g["iteration_name"]
        it["story_ids"].add(g["story_id"])
        for b in g["bugs"]:
            it["bug_ids"].add(b["bug_id"])
        it["n_bugs"] = len(it["bug_ids"])

    top_iterations = [
        {
            "iteration_id": iid,
            "iteration_name": info["name"][:60],
            "n_stories": len(info["story_ids"]),
            "n_bugs": info["n_bugs"],
        }
        for iid, info in sorted(iter_bug_counts.items(), key=lambda x: -x[1]["n_bugs"])
    ][:10]

    top = sorted(graph, key=lambda g: -g["bug_count"])
    top_bug_stories = [
        {
            "story_key": g["story_key"],
            "title": g["title"][:60],
            "iteration_id": g["iteration_id"],
            "bugs": g["bug_count"],
            "precise": sum(1 for b in g["bugs"] if b["linkage"] == "precise"),
            "weak_iteration": sum(
                1 for b in g["bugs"] if b["linkage"] == "weak:iteration"
            ),
        }
        for g in top
        if g["bug_count"]
    ][:15]

    summary = {
        "owner": args.owner,
        "n_stories": len(stories),
        "n_bugs": len(bugs),
        "n_iterations": len(iter_meta),
        "n_stories_with_precise_bugs": n_precise_stories,
        "n_precise_links": precise_link_count,
        "n_stories_with_iteration_links": len({sid for sid in weak_links}),
        "n_iteration_links": weak_link_count,
        "n_stories_covered_total": n_covered_total,
        "coverage_precise_pct": round(100.0 * n_precise_stories / len(stories), 1),
        "coverage_iteration_only_pct": round(
            100.0
            * len({sid for sid in weak_links if sid not in precise_links})
            / len(stories),
            1,
        ),
        "coverage_total_pct": round(100.0 * n_covered_total / len(stories), 1),
        "iteration_match_accuracy_on_precise": round(iter_accuracy, 3),
        "same_iter_on_precise": same_iter_on_precise,
        "diff_iter_on_precise": diff_iter_on_precise,
        "top_bug_stories": top_bug_stories,
        "top_iterations": top_iterations,
        "headline_task_type_note": "per-task_type bug-rate 用宽集重算需 Brief C 产物 story_task_types.json 完成后进行",
        "note": "weak:iteration = 同 sprint 撮合；many-to-many，只做聚合，不做 per-story 精确断言",
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({"summary": summary, "stories": graph}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
