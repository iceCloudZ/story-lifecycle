"""结果轴一期 A：bug ↔ story 关联图谱。

实测：TAPD bug 的正向 ``story_id`` 字段在本 workspace 全空（0/142），
故 TapdRelationResolver（正向）无效。改走【反向】：对每个 story 调
``get_related_bugs(story_id)`` → bug_id 列表（覆盖率 ~35%），再 enrich bug 详情。

产出：
  - ``scripts/out/bug_story_graph.json`` —— story → [bug...] 图谱 + summary
  - 控制台 summary：bug-rate、bug 磁铁 story

用法：
  python scripts/bug_story_graph.py --owner 赵子豪 --workspace-id 44381896 \\
      [--limit-stories N] [--no-detail] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[1]                       # packages/story-miner
sys.path.insert(0, str(_PROJ))                                     # miner
sys.path.insert(0, str(_PROJ.parent / "story-lifecycle" / "src"))  # story_lifecycle

from story_lifecycle.sources.tapd_source import TapdSource  # noqa: E402


def fetch_stories(src: TapdSource, owner: str, story_status: str, limit: int):
    """按 custom_field_25（后端开发）= owner 直查 story corpus。

    实测 ~150（≈ story.db 的 147）。比 TapdSource.fetch_pending 的
    "父 story + 按 owner 子任务" 逻辑（只拿 20）覆盖全——后者漏掉大多数。
    custom_field_25 是 corpus 口径。
    """
    params = {
        "entity_type": "stories",
        "limit": 500,
        "custom_field_25": owner.rstrip(";"),
    }
    if story_status != "*":
        params["status"] = story_status
    raw = src._api.get_stories(params) or []
    items = [src._parse_story(r.get("Story", r)) for r in raw]
    return items[:limit] if limit else items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", default="赵子豪")
    ap.add_argument("--workspace-id", default="44381896")
    ap.add_argument("--story-status", default="*", help="* = 全状态")
    ap.add_argument("--limit-stories", type=int, default=0, help="0 = 全部")
    ap.add_argument("--no-detail", action="store_true", help="跳过 bug 详情 enrich（更快）")
    ap.add_argument("--out", default=str(_PROJ / "scripts" / "out" / "bug_story_graph.json"))
    args = ap.parse_args()

    src = TapdSource(
        {
            "workspace_id": args.workspace_id,
            "owner": args.owner,
            "story_status": args.story_status,
            "bug_status": "*",
        }
    )
    print(f"fetching stories (custom_field_25={args.owner}, status={args.story_status}) ...")
    stories = fetch_stories(src, args.owner, args.story_status, args.limit_stories)
    print(f"  stories: {len(stories)}")

    bug_cache: dict[str, dict] = {}
    graph: list[dict] = []
    n_with_bugs = 0
    n_bugs_total = 0

    for i, s in enumerate(stories, 1):
        sid = s.id
        rel = src._api.get_related_bugs(sid) or []
        bug_ids = [r.get("bug_id") for r in rel if r.get("bug_id")]

        bugs: list[dict] = []
        if bug_ids:
            n_with_bugs += 1
            n_bugs_total += len(bug_ids)
            for bid in bug_ids:
                if args.no_detail:
                    bugs.append({"bug_id": bid})
                    continue
                if bid not in bug_cache:
                    raw = src._api.get_bug_detail(bid)
                    f = (raw.get("Bug", raw) if raw else {}) or {}
                    bug_cache[bid] = {
                        "bug_id": bid,
                        "title": f.get("title", ""),
                        "severity": f.get("severity", ""),
                        "status": f.get("status", ""),
                        "created": f.get("created", ""),
                        "resolved": f.get("resolved", ""),
                        "closed": f.get("closed", ""),
                        "iteration_id": f.get("iteration_id", ""),
                    }
                bugs.append(bug_cache[bid])

        graph.append(
            {
                "story_id": sid,
                "story_key": f"tapd-{sid}",
                "title": s.title,
                "iteration_id": s.extra.get("iteration_id", ""),
                "status": s.status,
                "bug_count": len(bug_ids),
                "bugs": bugs,
            }
        )
        if i % 20 == 0:
            print(f"  ...{i}/{len(stories)} | {n_with_bugs} with bugs | {n_bugs_total} bug-links")

    top = sorted(graph, key=lambda g: -g["bug_count"])
    summary = {
        "owner": args.owner,
        "n_stories": len(stories),
        "n_with_bugs": n_with_bugs,
        "n_bug_links": n_bugs_total,
        "pct_with_bugs": round(100.0 * n_with_bugs / len(stories), 1) if stories else 0,
        "note": "反向 get_related_bugs；正向 bug.story_id 在本 workspace 全空",
        "top_bug_stories": [
            {"story_key": g["story_key"], "title": g["title"][:60], "bugs": g["bug_count"]}
            for g in top
            if g["bug_count"]
        ][:10],
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
