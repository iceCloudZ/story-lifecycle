"""Brief I：结果轴二期（深化）分析。

输入：
  - scripts/out/bug_story_graph.json
  - scripts/out/story_task_types.json
  - scripts/out/story_commits_inferred.json
  - scripts/out/known_magnet_commits.json

输出：
  - scripts/out/result_axis_phase2.json
  - scripts/out/result_axis_phase2.md

分析项：
  1. bug-prone 代码模式（按 task_type）
  2. cycle-time（bug created→resolved）按 task_type/severity
  3. code-survival/churn（高频改动文件）
  4. bug→fix-commit 推断
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

_PROJ = Path(__file__).resolve().parents[1]
OUT_DIR = _PROJ / "scripts" / "out"
BASE = Path("D:/hc-all")
REPOS = [
    "hc-order", "hc-user", "hc-risk-management", "hc-message", "hc-config",
    "hc-limit", "hc-third-party", "hc-coupon", "hc-marketing", "hc-callback",
    "hc-gateway", "hc-job", "hc-audit", "hc-aiops", "hc-pytest",
    "story-board", "ys-frame-parent",
]


def load_json(name: str) -> dict | list:
    with open(OUT_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def run_git(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def repo_exists(repo: str) -> bool:
    return (BASE / repo / ".git").is_dir()


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def hours_between(a: str | None, b: str | None) -> float | None:
    dt_a = parse_dt(a)
    dt_b = parse_dt(b)
    if dt_a and dt_b:
        return round((dt_b - dt_a).total_seconds() / 3600.0, 2)
    return None


def severity_rank(sev: str) -> int:
    """致命>严重>一般>提示，数字越小越严重。"""
    mapping = {"致命": 1, "严重": 2, "一般": 3, "提示": 4, "normal": 3, "轻微": 4}
    return mapping.get(sev.strip(), 3)


def task_type_of(story_key: str, type_map: dict) -> str:
    return type_map.get(story_key, "unknown")


def collect_commits_for_stories(story_keys: set[str], inferred: list[dict], known: list[dict]) -> list[dict]:
    """聚合指定 stories 的全部 commits（含 repo/files）。"""
    commits = []
    for item in inferred + known:
        if item["story_key"] in story_keys:
            for c in item.get("commits", []):
                commits.append({
                    "story_key": item["story_key"],
                    "repo": item.get("repo") or c.get("repo"),
                    "sha": c["sha"],
                    "msg": c["msg"],
                    "files": c.get("files", []),
                })
    return commits


def bug_prone_patterns(task_type: str, stories: list[dict], type_map: dict, inferred: list[dict], known: list[dict]) -> list[dict]:
    """识别某 task_type 下高频改动且关联 bug 的文件/模式。"""
    keys = {s["story_key"] for s in stories if task_type_of(s["story_key"], type_map) == task_type}
    if not keys:
        return []
    commits = collect_commits_for_stories(keys, inferred, known)
    if not commits:
        return []

    file_commits = defaultdict(list)
    for c in commits:
        for f in c["files"]:
            file_commits[f].append(c)

    # 关联 bug 数量
    story_bugs = {s["story_key"]: s["bug_count"] for s in stories}
    file_bugs = Counter()
    for f, cs in file_commits.items():
        touched_stories = {c["story_key"] for c in cs}
        for sk in touched_stories:
            file_bugs[f] += story_bugs.get(sk, 0)

    patterns = []
    for f, n in Counter({f: len(cs) for f, cs in file_commits.items()}).most_common(15):
        patterns.append({
            "file": f,
            "commit_count": n,
            "bug_weight": file_bugs.get(f, 0),
            "repos": list(dict.fromkeys(c["repo"] for c in file_commits[f] if c["repo"])),
        })
    return patterns


def cycle_time_analysis(stories: list[dict], type_map: dict) -> dict:
    """按 task_type 和 severity 聚合 bug cycle time。"""
    by_type = defaultdict(list)
    by_severity = defaultdict(list)
    for s in stories:
        tt = task_type_of(s["story_key"], type_map)
        for b in s.get("bugs", []):
            h = hours_between(b.get("created"), b.get("resolved"))
            if h is None:
                h = hours_between(b.get("created"), b.get("closed"))
            if h is None or h < 0:
                continue
            sev = b.get("severity", "unknown")
            by_type[tt].append(h)
            by_severity[sev].append(h)

    def stats(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0, "median": 0, "p90": 0, "mean": 0}
        s = sorted(vals)
        p90 = s[min(int(len(s) * 0.9), len(s) - 1)]
        return {"n": len(vals), "median": round(median(vals), 1), "p90": round(p90, 1), "mean": round(sum(vals) / len(vals), 1)}

    return {
        "by_task_type": {k: stats(v) for k, v in by_type.items()},
        "by_severity": {k: stats(v) for k, v in by_severity.items()},
    }


def code_churn(task_type: str, stories: list[dict], type_map: dict, inferred: list[dict], known: list[dict]) -> dict:
    """统计某 task_type 的代码改动范围与重复改动。"""
    keys = {s["story_key"] for s in stories if task_type_of(s["story_key"], type_map) == task_type}
    commits = collect_commits_for_stories(keys, inferred, known)
    if not commits:
        return {"n_commits": 0, "n_files": 0, "top_files": [], "avg_files_per_commit": 0}

    file_count = Counter()
    for c in commits:
        for f in c["files"]:
            file_count[f] += 1

    return {
        "n_commits": len(commits),
        "n_files": len(file_count),
        "top_files": [{"file": f, "commits": n} for f, n in file_count.most_common(10)],
        "avg_files_per_commit": round(sum(len(c["files"]) for c in commits) / len(commits), 2),
    }


def infer_bug_fix_commit(bug: dict, repo: str | None, story_title: str) -> list[dict]:
    """根据 bug 时间窗和关键词推断 fix commit。"""
    if not repo or not repo_exists(repo):
        return []
    created = bug.get("created")
    resolved = bug.get("resolved") or bug.get("closed")
    if not created or not resolved:
        return []

    dt_created = parse_dt(created)
    dt_resolved = parse_dt(resolved)
    if not dt_created or not dt_resolved:
        return []
    # fix 通常在 created 和 resolved 之间，或 resolved 前后
    since = (dt_created - timedelta(days=1)).strftime("%Y-%m-%d")
    until = (dt_resolved + timedelta(days=3)).strftime("%Y-%m-%d")

    rpath = BASE / repo
    out = run_git(["git", "-C", str(rpath), "log", "master", "--since", since, "--until", until, "--format=%H|%aI|%s"])
    candidates = []
    bug_title = (bug.get("title") or "").lower()
    kws = [w for w in re.split(r"[^a-z0-9\u4e00-\u9fff]+", bug_title) if len(w) >= 2 and w not in {"bug", "fix", "the", "and"}]
    for line in out.stdout.strip().splitlines():
        if "|" not in line:
            continue
        sha, dt, msg = line.split("|", 2)
        msg_lower = msg.lower()
        score = 0
        if "fix" in msg_lower:
            score += 2
        for kw in kws:
            if kw in msg_lower:
                score += 1
        if score > 0:
            candidates.append({"sha": sha[:12], "datetime": dt, "msg": msg, "score": score})
    candidates.sort(key=lambda x: (-x["score"], x["datetime"]))
    return candidates[:5]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(OUT_DIR / "result_axis_phase2.json"))
    ap.add_argument("--report", default=str(OUT_DIR / "result_axis_phase2.md"))
    args = ap.parse_args()

    graph = load_json("bug_story_graph.json")
    stories = graph["stories"]
    task_types = load_json("story_task_types.json")
    type_map = {r["story_key"]: r["task_type"] for r in task_types}
    inferred = load_json("story_commits_inferred.json")
    known = load_json("known_magnet_commits.json")

    all_types = sorted(set(type_map.values()) | {"unknown"})

    # 1. bug-prone 模式
    print("analyzing bug-prone patterns...")
    patterns_by_type = {}
    for tt in all_types:
        patterns_by_type[tt] = bug_prone_patterns(tt, stories, type_map, inferred, known)

    # 2. cycle-time
    print("analyzing cycle time...")
    ct = cycle_time_analysis(stories, type_map)

    # 3. code churn
    print("analyzing code churn...")
    churn_by_type = {}
    for tt in all_types:
        churn_by_type[tt] = code_churn(tt, stories, type_map, inferred, known)

    # 4. bug→fix-commit：只对磁铁 story 的 bug 做（避免 234 次全跑）
    print("inferring bug fix commits for magnets...")
    bug_fixes = []
    story_commits_map = {item["story_key"]: item for item in inferred + known}
    magnet_keys = {s["story_key"] for s in sorted(stories, key=lambda x: -x["bug_count"])[:11]}
    for sk in magnet_keys:
        story = next((s for s in stories if s["story_key"] == sk), None)
        item = story_commits_map.get(sk)
        if not story or not item:
            continue
        repo = item.get("repo")
        for b in story.get("bugs", []):
            fixes = infer_bug_fix_commit(b, repo, story["title"])
            if fixes:
                bug_fixes.append({
                    "story_key": sk,
                    "bug_id": b["bug_id"],
                    "bug_title": b.get("title", ""),
                    "repo": repo,
                    "candidates": fixes,
                })

    result = {
        "n_stories": len(stories),
        "n_bugs": graph["summary"]["n_bug_links"],
        "patterns_by_task_type": patterns_by_type,
        "cycle_time": ct,
        "churn_by_task_type": churn_by_type,
        "bug_fix_commits": bug_fixes,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}")

    # 报告
    lines = ["# Brief I 报告 — 结果轴二期（深化）\n"]
    lines.append(f"- story 总数：{len(stories)}\n")
    lines.append(f"- bug-link 总数：{graph['summary']['n_bug_links']}\n")

    lines.append("\n## 1. bug-prone 代码模式（按 task_type）\n")
    for tt in all_types:
        pts = patterns_by_type.get(tt, [])
        if not pts:
            continue
        lines.append(f"\n### {tt}\n")
        lines.append("| file | commit_count | bug_weight | repos |")
        lines.append("|------|--------------|------------|-------|")
        for p in pts[:10]:
            repos = ",".join(p["repos"][:3])
            lines.append(f"| {p['file']} | {p['commit_count']} | {p['bug_weight']} | {repos} |")

    lines.append("\n## 2. bug cycle-time（created → resolved，小时）\n")
    lines.append("\n### 按 task_type\n")
    lines.append("| task_type | n | median | p90 | mean |")
    lines.append("|-----------|---|--------|-----|------|")
    for tt in all_types:
        st = ct["by_task_type"].get(tt, {"n": 0, "median": 0, "p90": 0, "mean": 0})
        if st["n"]:
            lines.append(f"| {tt} | {st['n']} | {st['median']} | {st['p90']} | {st['mean']} |")
    lines.append("\n### 按 severity\n")
    lines.append("| severity | n | median | p90 | mean |")
    lines.append("|----------|---|--------|-----|------|")
    for sev, st in sorted(ct["by_severity"].items(), key=lambda x: severity_rank(x[0])):
        if st["n"]:
            lines.append(f"| {sev} | {st['n']} | {st['median']} | {st['p90']} | {st['mean']} |")

    lines.append("\n## 3. code churn（按 task_type）\n")
    lines.append("| task_type | n_commits | n_files | avg_files/commit | top_file |")
    lines.append("|-----------|-----------|---------|------------------|----------|")
    for tt in all_types:
        ch = churn_by_type.get(tt, {})
        top_files = ch.get("top_files", [])
        top = top_files[0].get("file", "-") if top_files else "-"
        lines.append(f"| {tt} | {ch.get('n_commits',0)} | {ch.get('n_files',0)} | {ch.get('avg_files_per_commit',0)} | {top} |")

    lines.append("\n## 4. bug→fix-commit 推断（top 11 磁铁）\n")
    lines.append(f"- 共推断 {len(bug_fixes)} 个 bug 的 fix commit 候选\n")
    for bf in bug_fixes[:15]:
        lines.append(f"\n### {bf['bug_id']} ({bf['story_key']})\n")
        lines.append(f"- repo: {bf['repo']} | title: {bf['bug_title'][:60]}\n")
        for c in bf["candidates"]:
            lines.append(f"- `{c['sha']}` ({c['score']} pts) {c['msg'][:80]}")

    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.report}")


if __name__ == "__main__":
    main()
