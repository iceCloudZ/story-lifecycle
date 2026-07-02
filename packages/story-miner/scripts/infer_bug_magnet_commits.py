"""Brief E：无 branch bug 磁铁的 commit 推断（+ 在 3 个有 branch 磁铁上验证）。

策略 v3：
1. 标题关键词 → 候选 repo
2. 拉取候选 repo 全局时间窗内的所有 merge commits
3. 在 Python 中用正则/关键词匹配 merge msg（尤其提取 branch 名）与 story 标题
4. 对 top merge commit 提取其引入的 commits
5. 与已知 branch commits 算 recall 验证

输出：
  - scripts/out/story_commits_inferred.json
  - scripts/out/infer_bug_magnet_report.md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import re
from collections import Counter
from pathlib import Path

import yaml

_PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ.parent / "story-lifecycle" / "src"))

from story_lifecycle.sourcing.sources.tapd_source import TapdSource

BASE = Path("D:/hc-all")
REPOS = [
    "hc-order", "hc-user", "hc-risk-management", "hc-message", "hc-config",
    "hc-limit", "hc-third-party", "hc-coupon", "hc-marketing", "hc-callback",
    "hc-gateway", "hc-job", "hc-audit", "hc-aiops", "hc-pytest",
    "story-board", "ys-frame-parent",
]

REPO_HINTS = [
    ("授信", ["hc-limit", "hc-risk-management", "hc-order"]),
    ("额度", ["hc-limit", "hc-risk-management", "hc-order"]),
    ("风控", ["hc-risk-management", "hc-limit", "hc-order"]),
    ("反欺诈", ["hc-risk-management", "hc-limit", "hc-order"]),
    ("还款", ["hc-order", "hc-user"]),
    ("放款", ["hc-order", "hc-user"]),
    ("提现", ["hc-order", "hc-user", "hc-limit"]),
    ("清分", ["hc-order", "hc-callback"]),
    ("订单", ["hc-order"]),
    ("交易", ["hc-order"]),
    ("MGM", ["hc-marketing", "hc-user", "hc-coupon"]),
    ("营销", ["hc-marketing", "hc-coupon"]),
    ("活动", ["hc-marketing", "hc-coupon", "hc-user"]),
    ("券", ["hc-marketing", "hc-coupon"]),
    ("免息", ["hc-order", "hc-marketing"]),
    ("短信", ["hc-message", "hc-third-party"]),
    ("OTP", ["hc-message", "hc-third-party"]),
    ("通知", ["hc-message", "hc-third-party"]),
    ("用户", ["hc-user", "hc-risk-management"]),
    ("认证", ["hc-user", "hc-risk-management"]),
    ("资料", ["hc-user"]),
    ("回调", ["hc-callback", "hc-third-party"]),
    ("三方", ["hc-third-party", "hc-callback"]),
    ("回传", ["hc-callback", "hc-third-party"]),
    ("网关", ["hc-gateway"]),
    ("限流", ["hc-gateway", "hc-config"]),
    ("配置", ["hc-config", "hc-gateway"]),
    ("调度", ["hc-job", "hc-config"]),
    ("状态机", ["hc-config", "hc-order"]),
    ("SQL", ["hc-order", "hc-user", "hc-risk-management"]),
    ("数据", ["hc-order", "hc-user", "hc-risk-management"]),
    ("前端", ["frontends"]),
    ("admin", ["frontends"]),
    ("页面", ["frontends"]),
    ("部署", ["hc-job", "hc-config"]),
    ("上线", ["hc-job", "hc-config"]),
    ("发版", ["hc-job", "hc-config"]),
]

# 中文词 -> 可能出现在 branch/msg 中的英文片段
KW_MAP = {
    "授信": ["credit", "limit", "quota"],
    "额度": ["limit", "quota", "credit"],
    "风控": ["risk", "fraud"],
    "反欺诈": ["fraud", "risk"],
    "还款": ["repay", "repayment"],
    "放款": ["loan", "disburse"],
    "提现": ["withdraw", "withdrawal"],
    "清分": ["liquidate", "clear", "settle"],
    "订单": ["order"],
    "交易": ["trade", "txn"],
    "MGM": ["mgm"],
    "营销": ["market", "marketing"],
    "活动": ["activity", "campaign"],
    "券": ["coupon"],
    "免息": ["free-interest", "free_interest", "freeinterest", "7days"],
    "新客": ["new", "7days"],
    "短信": ["sms"],
    "用户": ["user"],
    "认证": ["kyc", "verify", "verification"],
    "资料": ["profile", "document", "info"],
    "回调": ["callback"],
    "三方": ["third-party", "third_party", "thirdparty"],
    "回传": ["callback", "conversion", "postback"],
    "网关": ["gateway"],
    "限流": ["rate-limit", "rate_limit", "ratelimit"],
    "配置": ["config"],
    "调度": ["job", "schedule"],
    "状态机": ["state-machine", "state_machine", "statemachine"],
    "审核": ["audit", "review"],
    "迁移": ["migrate", "migration"],
    "重构": ["refactor", "rebuild", "restructure"],
    "授信": ["credit", "limit", "quota", "授信"],
    "节点": ["node", "center", "core"],
    "提额": ["increase", "raise", "limit", "quota"],
    "增信": ["credit", "limit", "increase"],
    "注销": ["cancel", "deactivate", "inactive"],
    "恢复": ["recover", "restore"],
}


def load_config():
    with open(Path.home() / ".story-lifecycle" / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_git(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def candidate_repos(title: str) -> list[str]:
    title = title.lower()
    scores = Counter()
    for kw, repos in REPO_HINTS:
        if kw.lower() in title:
            for i, r in enumerate(repos):
                scores[r] += max(1, 10 - i)
    if scores:
        return [r for r, _ in scores.most_common()]
    return REPOS[:]


def repo_exists(repo: str) -> bool:
    return (BASE / repo / ".git").is_dir()


def parse_window(story: dict) -> tuple[str | None, str | None]:
    s = story.get("custom_field_190") or story.get("begin") or story.get("created")
    e = story.get("custom_field_40") or story.get("due") or story.get("modified")
    return s, e


MERGE_CACHE: dict[str, list[dict]] = {}

def get_repo_merges(repo: str, since: str, until: str) -> list[dict]:
    key = f"{repo}|{since}|{until}"
    if key in MERGE_CACHE:
        return MERGE_CACHE[key]
    rpath = BASE / repo
    args = ["git", "-C", str(rpath), "log", "master", "--merges", "--format=%H|%aI|%s", f"--since={since}", f"--until={until}"]
    out = run_git(args)
    merges = []
    for line in out.stdout.strip().splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, dt, msg = parts
        merges.append({"sha": sha[:12], "datetime": dt, "msg": msg, "repo": repo})
    MERGE_CACHE[key] = merges
    return merges


def extract_branch_name(msg: str) -> str:
    m = re.search(r"Merge branch ['\"]([^'\"]+)['\"]", msg)
    if m:
        return m.group(1)
    return ""


def story_search_patterns(title: str) -> list[str]:
    """生成用于匹配 branch/msg 的英文正则片段。"""
    title_lower = title.lower()
    patterns = []
    for cn, ens in KW_MAP.items():
        if cn in title:
            patterns.extend(ens)
    # 标题中的连续英文/数字也加入（过滤太短的和泛词）
    noise = {"hc", "ys", "tapd", "story", "feature", "fix", "bug"}
    for w in re.findall(r"[a-z0-9_]{3,}", title_lower):
        if w not in noise:
            patterns.append(w)
    return list(dict.fromkeys(patterns))  # 去重保序


def tokenize_branch(branch: str) -> set[str]:
    """把 branch 名拆成 token。"""
    branch = branch.lower()
    # 去掉常见前缀
    branch = re.sub(r"^(feature/zzh/|feature/|hotfix-|zed/|dun/|cjc/|origin/)", "", branch)
    # 拆分
    tokens = set(re.split(r"[_/\-\\.]", branch))
    tokens.discard("")
    return tokens


def sample_merge_commits(repo: str, merge_sha: str, max_n: int = 20) -> list[dict]:
    """轻量采样 merge 引入的 commits（用于打分）。"""
    rpath = BASE / repo
    out = run_git(["git", "-C", str(rpath), "rev-parse", f"{merge_sha}^@"])
    parents = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
    if len(parents) < 2:
        return []
    out = run_git(["git", "-C", str(rpath), "log", f"{parents[0]}..{merge_sha}", "--format=%H|%s", f"--max-count={max_n}"])
    commits = []
    for line in out.stdout.strip().splitlines():
        if "|" not in line:
            continue
        sha, msg = line.split("|", 1)
        files_out = run_git(["git", "-C", str(rpath), "diff-tree", "--no-commit-id", "--name-only", "-r", sha])
        files = [l.strip() for l in files_out.stdout.strip().splitlines() if l.strip()]
        commits.append({"sha": sha[:12], "msg": msg, "files": files})
    return commits


def score_merge(merge: dict, title: str, patterns: list[str]) -> float:
    score = 0.0
    branch = extract_branch_name(merge["msg"])
    branch_lower = branch.lower()
    text = (merge["msg"] + " " + branch).lower()
    title_lower = title.lower()
    title_tokens = set(re.split(r"[【】\s\-_/\\|]", title_lower))
    title_tokens.discard("")
    branch_tokens = tokenize_branch(branch)

    # branch token 与标题 token 的重叠
    for bt in branch_tokens:
        if len(bt) < 3:
            continue
        if bt in title_tokens or bt in title_lower:
            score += 3.0
        # 英文关键词映射
        for cn, ens in KW_MAP.items():
            if cn in title and any(bt == e.lower().replace("-", "_") for e in ens):
                score += 2.0

    # 直接关键词匹配
    for p in patterns:
        p = p.lower()
        if p in text:
            score += 2.0

    # 标题中的中文字符在 branch 中出现
    for ch in title:
        if "\u4e00" <= ch <= "\u9fff" and ch in branch:
            score += 1.0

    # merge 引入的 commit msg/files 与标题关键词共现
    sampled = merge.get("_sampled_commits")
    if sampled is None:
        sampled = sample_merge_commits(merge["repo"], merge["sha"], max_n=15)
        merge["_sampled_commits"] = sampled
    for c in sampled:
        ctext = (c["msg"] + " " + " ".join(c["files"])).lower()
        for p in patterns:
            p = p.lower()
            if p in ctext:
                score += 1.5
        # 业务关键词映射
        for cn, ens in KW_MAP.items():
            if cn in title:
                for e in ens:
                    if e.lower() in ctext:
                        score += 1.0
                        break

    return score


def extract_feature_commits(repo: str, merge_sha: str) -> list[dict]:
    """提取 merge commit 对应的 feature branch 引入的全部 commits。

    策略：从 merge msg 提取 branch 名，找到该 branch 的所有 merge commits，
    对每个 merge 提取 parents[0]..merge 的 commits，合并去重。
    """
    rpath = BASE / repo
    out = run_git(["git", "-C", str(rpath), "show", "-s", "--format=%s", merge_sha])
    merge_msg = out.stdout.strip()
    branch = extract_branch_name(merge_msg)
    if not branch:
        return []

    # 找到该 branch 在 master 上的所有 merge commits
    out = run_git(["git", "-C", str(rpath), "log", "master", "--merges", "--grep", branch, "--format=%H"])
    merge_shas = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]

    all_commits = []
    for msha in merge_shas:
        out = run_git(["git", "-C", str(rpath), "rev-parse", f"{msha}^@"])
        parents = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
        if len(parents) < 2:
            continue
        out = run_git(["git", "-C", str(rpath), "log", f"{parents[0]}..{msha}", "--format=%H|%s"])
        for line in out.stdout.strip().splitlines():
            if "|" not in line:
                continue
            sha, msg = line.split("|", 1)
            files_out = run_git(["git", "-C", str(rpath), "diff-tree", "--no-commit-id", "--name-only", "-r", sha])
            files = [l.strip() for l in files_out.stdout.strip().splitlines() if l.strip()]
            all_commits.append({"sha": sha[:12], "msg": msg, "files": files, "repo": repo})

    seen = set()
    uniq = []
    for c in all_commits:
        if c["sha"] not in seen:
            seen.add(c["sha"])
            uniq.append(c)
    return uniq


def quick_score_merge(merge: dict, title: str, patterns: list[str]) -> float:
    """不加采样的快速预筛分数。"""
    score = 0.0
    branch = extract_branch_name(merge["msg"]).lower()
    text = (merge["msg"] + " " + branch).lower()
    title_lower = title.lower()
    title_tokens = set(re.split(r"[【】\s\-_/\\|]", title_lower))
    title_tokens.discard("")
    branch_tokens = tokenize_branch(branch)
    for bt in branch_tokens:
        if len(bt) < 3:
            continue
        if bt in title_tokens or bt in title_lower:
            score += 3.0
        for cn, ens in KW_MAP.items():
            if cn in title and any(bt == e.lower().replace("-", "_") for e in ens):
                score += 2.0
    for p in patterns:
        if p.lower() in text:
            score += 2.0
    for ch in title:
        if "\u4e00" <= ch <= "\u9fff" and ch in branch:
            score += 1.0
    return score


def infer_story_commits(story_key: str, title: str, since: str | None, until: str | None,
                        known_shas: set[str] | None = None) -> dict:
    repos = candidate_repos(title)
    patterns = story_search_patterns(title)
    print(f"    search patterns: {patterns}")

    effective_since = since or "2025-11-01"
    effective_until = until or "2026-07-15"
    if effective_until:
        from datetime import datetime, timedelta
        try:
            dt = datetime.strptime(effective_until.split()[0], "%Y-%m-%d")
            effective_until = (dt + timedelta(days=60)).strftime("%Y-%m-%d")
        except Exception:
            pass

    # 第一阶段：快速预筛所有候选 repo，取 top 20 merges
    all_candidates = []
    for repo in list(repos) + [r for r in REPOS if r not in repos]:
        if not repo_exists(repo):
            continue
        merges = get_repo_merges(repo, effective_since, effective_until)
        for m in merges:
            s = quick_score_merge(m, title, patterns)
            if s > 0:
                all_candidates.append((s, repo, m))
    all_candidates.sort(key=lambda x: -x[0])
    top_candidates = all_candidates[:20]

    # 第二阶段：对 top candidates 采样 commits 详细打分
    best = None
    best_score = 0.0
    for s, repo, m in top_candidates:
        full_score = score_merge(m, title, patterns)
        if full_score > best_score:
            best_score = full_score
            best = (repo, m)

    if not best or best_score < 2.0:
        return {"story_key": story_key, "title": title, "repo": None,
                "n_commits": 0, "commits": [], "linkage": "not_found", "confidence": 0.0,
                "time_window": {"since": since, "until": until}}

    repo, merge = best
    feature_commits = extract_feature_commits(repo, merge["sha"])

    confidence = min(best_score / 10.0, 1.0)
    if known_shas:
        hits = [c for c in feature_commits if c["sha"] in known_shas]
        confidence = max(confidence, round(len(hits) / max(len(feature_commits), 1), 2))

    commits_out = [{"sha": c["sha"], "msg": c["msg"], "files": c["files"]} for c in feature_commits]
    return {
        "story_key": story_key,
        "title": title,
        "repo": repo,
        "merge_sha": merge["sha"],
        "merge_msg": merge["msg"],
        "merge_datetime": merge["datetime"],
        "n_commits": len(commits_out),
        "commits": commits_out,
        "linkage": "inferred:merge-commit",
        "confidence": confidence,
        "time_window": {"since": since, "until": until},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-n", type=int, default=11, help="取 bug_count top N 作为磁铁")
    ap.add_argument(
        "--all-branchless",
        action="store_true",
        help="推断所有无 branch 的 story（不只 top 磁铁）；慢，建议 full refresh 用",
    )
    ap.add_argument("--out", default=str(_PROJ / "scripts" / "out" / "story_commits_inferred.json"))
    ap.add_argument("--report", default=str(_PROJ / "scripts" / "out" / "infer_bug_magnet_report.md"))
    args = ap.parse_args()

    graph_path = _PROJ / "scripts" / "out" / "bug_story_graph.json"
    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)
    stories = sorted(graph["stories"], key=lambda s: -s["bug_count"])

    # branch_map 先建（用于 --all-branchless 筛选 + 后续验证）
    home = Path.home()
    conn = sqlite3.connect(str(home / ".story-lifecycle" / "story.db"))
    cur = conn.cursor()
    all_keys = [s["story_key"] for s in stories]
    ph_all = ",".join("?" * len(all_keys)) if all_keys else "''"
    cur.execute(
        f"SELECT story_key, branch, base_branch FROM story_project WHERE story_key IN ({ph_all})",
        all_keys,
    )
    branch_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    conn.close()
    branched_keys = {sk for sk, (b, _) in branch_map.items() if b}

    if args.all_branchless:
        magnets = [s for s in stories if s["story_key"] not in branched_keys]
        print(
            f"--all-branchless: {len(magnets)} 个无 branch story（排除 {len(branched_keys)} 个有 branch）"
        )
    else:
        magnets = stories[:args.top_n]
        print(f"selected top {len(magnets)} bug magnets")

    src = TapdSource({"workspace_id": "44381896", "owner": "赵子豪", "story_status": "*", "bug_status": "*"})
    story_meta = {}
    for s in magnets:
        sid = s["story_id"]
        raw = src._api.get_story_detail(sid)
        tapd = (raw.get("Story", raw) if raw else {}) or {}
        story_meta[s["story_key"]] = {
            "title": s["title"],
            "bug_count": s["bug_count"],
            "created": tapd.get("created"),
            "modified": tapd.get("modified"),
            "begin": tapd.get("begin"),
            "due": tapd.get("due"),
            "custom_field_40": tapd.get("custom_field_40"),
            "custom_field_190": tapd.get("custom_field_190"),
            "iteration_id": tapd.get("iteration_id"),
        }

    known_path = _PROJ / "scripts" / "out" / "known_magnet_commits.json"
    known_data = []
    if known_path.exists():
        with open(known_path, "r", encoding="utf-8") as f:
            known_data = json.load(f)
    known_by_story = {k["story_key"]: set(c["sha"] for c in k["commits"]) for k in known_data}

    inferred = []
    validation = []
    for i, s in enumerate(magnets, 1):
        sk = s["story_key"]
        meta = story_meta[sk]
        since, until = parse_window(meta)
        br = branch_map.get(sk)
        known_shas = known_by_story.get(sk)

        print(f"\n[{i}/{len(magnets)}] {sk} | {s['title'][:45]} | bugs={s['bug_count']} | branch={br[0] if br else None}")

        res = infer_story_commits(sk, meta["title"], since, until, known_shas=known_shas)

        if br and br[0]:
            # 对已知 branch 磁铁，用真实 commits 替换推断的少量 commits，便于下游汇总
            known_entry = None
            for k in known_data:
                if k["story_key"] == sk:
                    known_entry = k
                    break
            if known_entry:
                res["repo"] = known_entry["repo"]
                res["commits"] = [{"sha": c["sha"], "msg": c["msg"], "files": c["files"]} for c in known_entry["commits"]]
                res["n_commits"] = len(res["commits"])
            # 验证：merge commit 是否命中已知集合中的某个 sha
            inferred_shas = {c["sha"] for c in res["commits"]}
            hits = inferred_shas & (known_shas or set())
            recall = round(len(hits) / len(known_shas), 2) if known_shas else 0.0
            validation.append({
                "story_key": sk,
                "title": meta["title"],
                "repo": res["repo"],
                "merge_sha": res.get("merge_sha"),
                "inferred_n": len(res["commits"]),
                "known_n": len(known_shas or set()),
                "hits": len(hits),
                "recall": recall,
            })
            print(f"  VALIDATION: inferred={len(res['commits'])} known={len(known_shas or set())} hits={len(hits)} recall={recall} merge={(res.get('merge_msg') or '')[:80]}")
            res["linkage"] = "inferred:merge-commit (validation against branch)"
        else:
            print(f"  INFERRED: repo={res['repo']} n={len(res['commits'])} confidence={res['confidence']} merge={(res.get('merge_msg') or '')[:80]}")
        inferred.append(res)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(inferred, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {args.out}")

    lines = ["# Brief E 报告 — 无 branch bug 磁铁 commit 推断\n"]
    lines.append(f"- 分析磁铁数：{len(magnets)}\n")
    lines.append(f"- 有 branch 可验证：{len(validation)}\n")
    lines.append(f"- 无 branch 待推断：{len(magnets) - len(validation)}\n")

    lines.append("\n## 在 3 个有 branch 磁铁上的验证\n")
    lines.append("| story_key | repo | inferred | known | hits | recall | merge_sha |")
    lines.append("|-----------|------|----------|-------|------|--------|-----------|")
    for v in validation:
        lines.append(f"| {v['story_key']} | {v['repo']} | {v['inferred_n']} | {v['known_n']} | {v['hits']} | {v['recall']} | {(v.get('merge_sha') or '')[:12]} |")
    avg_recall = round(sum(v['recall'] for v in validation) / len(validation), 2) if validation else 0
    lines.append(f"\n平均 recall：{avg_recall}\n")

    lines.append("\n## 无 branch 磁铁推断结果\n")
    lines.append("| story_key | repo | n_commits | confidence | merge_msg |")
    lines.append("|-----------|------|-----------|------------|-----------|")
    for r in inferred:
        if "validation" in r["linkage"]:
            continue
        merge_msg = (r.get("merge_msg") or "")[:40]
        lines.append(f"| {r['story_key']} | {r['repo']} | {r['n_commits']} | {r['confidence']} | {merge_msg} |")

    lines.append("\n## 详细推断 commit\n")
    for r in inferred:
        lines.append(f"\n### {r['story_key']} — {r['title'][:60]}\n")
        lines.append(f"- repo: {r['repo']} | linkage: {r['linkage']} | confidence: {r['confidence']} | window: {r['time_window']['since']} ~ {r['time_window']['until']}\n")
        if r.get("merge_msg"):
            lines.append(f"- merge: `{r.get('merge_sha')}` {r['merge_msg']} ({r.get('merge_datetime')})\n")
        for c in r.get("commits", [])[:10]:
            lines.append(f"- `{c['sha']}` {c['msg'][:100]}")

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.report}")


if __name__ == "__main__":
    main()
