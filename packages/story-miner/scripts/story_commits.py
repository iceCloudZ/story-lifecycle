"""结果轴一期 B：story → commit 关联（git）。

输入：~/.story-lifecycle/story.db 的 story_project 表（branch 非空）。
机制：
  1. 遍历 hc-all 的 17 个子仓，定位同名分支；
  2. 用 ``git log <branch> --not <base_branch> --name-only`` 取相对 base 的净提交；
  3. 分支存在但已合并的，在 ``<base_branch>`` 上搜 merge commit 兜底；
  4. 分支完全找不到的，全局在 master 上搜 merge commit 兜底。
输出：scripts/out/story_commits.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[1]

REPOS = [
    "hc-order",
    "hc-user",
    "hc-risk-management",
    "hc-message",
    "hc-config",
    "hc-limit",
    "hc-third-party",
    "hc-coupon",
    "hc-marketing",
    "hc-callback",
    "hc-gateway",
    "hc-job",
    "hc-audit",
    "hc-aiops",
    "hc-pytest",
    "story-board",
    "ys-frame-parent",
]


def run_git(repo_path: Path, args: list[str]) -> tuple[int, str, str]:
    cmd = ["git", "-C", str(repo_path)] + args
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.returncode, result.stdout, result.stderr


def parse_git_log(stdout: str) -> list[dict]:
    """解析 ``--format='>>>>COMMIT %H%n%s%n%b%n>>>>FILES' --name-only`` 的输出。"""
    commits: list[dict] = []
    current: dict | None = None
    state = "start"  # start -> msg -> body -> files

    for line in stdout.splitlines():
        if line.startswith(">>>>COMMIT "):
            if current is not None:
                current["body"] = current["body"].rstrip()
                commits.append(current)
            current = {"sha": line.split(" ", 1)[1].strip(), "msg": "", "body": "", "files": []}
            state = "msg"
            continue

        if current is None:
            continue

        if line == ">>>>FILES":
            state = "files"
            continue

        if state == "msg":
            current["msg"] = line
            state = "body"
            continue

        if state == "body":
            if line == "":
                continue
            if current["body"]:
                current["body"] += "\n" + line
            else:
                current["body"] = line
            continue

        if state == "files":
            if line == "":
                continue
            current["files"].append(line)

    if current is not None:
        current["body"] = current["body"].rstrip()
        commits.append(current)

    return commits


def get_branch_commits(repo_path: Path, branch: str, base_branch: str) -> list[dict]:
    base = base_branch or "master"
    rc, stdout, _ = run_git(
        repo_path,
        [
            "log",
            branch,
            "--not",
            base,
            "--format=>>>>COMMIT %H%n%s%n%b%n>>>>FILES",
            "--name-only",
        ],
    )
    if rc != 0:
        return []
    return parse_git_log(stdout)


def find_merge_commit(repo_path: Path, branch: str, base_branch: str) -> str | None:
    base = base_branch or "master"
    rc, stdout, _ = run_git(
        repo_path, ["log", base, "--grep", branch, "--oneline", "-n", "10"]
    )
    if rc != 0 or not stdout.strip():
        return None
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if parts:
            return parts[0]
    return None


def get_merge_commit_info(repo_path: Path, merge_sha: str) -> dict:
    """获取 merge commit 的元信息（不含文件列表）。"""
    rc, stdout, _ = run_git(
        repo_path,
        ["show", "--format=>>>>COMMIT %H%n%s%n%b", "--no-patch", merge_sha],
    )
    if rc != 0:
        return {}
    commits = parse_git_log(stdout)
    return commits[0] if commits else {}


def get_merge_commit_files(repo_path: Path, merge_sha: str) -> list[str]:
    """获取 merge commit 相对于第一父提交引入的文件列表。"""
    rc, stdout, _ = run_git(
        repo_path,
        ["diff", f"{merge_sha}^..{merge_sha}", "--name-only"],
    )
    if rc != 0:
        return []
    return [line for line in stdout.splitlines() if line]


def get_merge_commit_commits(repo_path: Path, merge_sha: str) -> list[dict]:
    info = get_merge_commit_info(repo_path, merge_sha)
    if not info:
        return []
    info["files"] = get_merge_commit_files(repo_path, merge_sha)
    return [info]


def resolve_story_commits(
    hc_all: Path, story_key: str, branch: str, base_branch: str
) -> dict:
    base = base_branch or "master"

    # 1. 遍历所有仓：先定位 branch，取相对 base 的净提交；空则在本仓 base 上找 merge commit
    for repo_name in REPOS:
        repo_path = hc_all / repo_name
        if not repo_path.exists():
            continue

        rc, _, _ = run_git(repo_path, ["rev-parse", "--verify", branch])
        if rc != 0:
            continue

        commits = get_branch_commits(repo_path, branch, base)
        if commits:
            return {
                "story_key": story_key,
                "branch": branch,
                "base_branch": base,
                "repo": repo_path.name,
                "n_commits": len(commits),
                "commits": commits,
                "linkage": "branch",
            }

        # 分支存在但已合并到 base，尝试用 merge commit 兜底（在 base 分支上搜）
        merge_sha = find_merge_commit(repo_path, branch, base)
        if merge_sha:
            commits = get_merge_commit_commits(repo_path, merge_sha)
            if commits:
                return {
                    "story_key": story_key,
                    "branch": branch,
                    "base_branch": base,
                    "repo": repo_path.name,
                    "n_commits": len(commits),
                    "commits": commits,
                    "linkage": "merge_commit",
                }

    # 2. 全局兜底：branch 完全不存在时，在所有仓的 master 上搜 merge commit
    for repo_name in REPOS:
        repo_path = hc_all / repo_name
        if not repo_path.exists():
            continue
        merge_sha = find_merge_commit(repo_path, branch, "master")
        if merge_sha:
            commits = get_merge_commit_commits(repo_path, merge_sha)
            if commits:
                return {
                    "story_key": story_key,
                    "branch": branch,
                    "base_branch": base,
                    "repo": repo_path.name,
                    "n_commits": len(commits),
                    "commits": commits,
                    "linkage": "merge_commit",
                }

    return {
        "story_key": story_key,
        "branch": branch,
        "base_branch": base,
        "repo": None,
        "n_commits": 0,
        "commits": [],
        "linkage": "not_found",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.expanduser("~/.story-lifecycle/story.db"))
    ap.add_argument("--hc-all", default="D:/hc-all", type=Path)
    ap.add_argument(
        "--out", default=str(_PROJ / "scripts" / "out" / "story_commits.json")
    )
    args = ap.parse_args()

    hc_all = Path(args.hc_all)

    conn = sqlite3.connect(args.db)
    raw_rows = conn.execute(
        "select story_key, branch, base_branch from story_project "
        "where branch is not null and branch != ''"
    ).fetchall()
    rows = list(dict.fromkeys(raw_rows))  # 保留顺序去重
    print(f"story_project rows: {len(raw_rows)} | unique (story_key, branch, base_branch): {len(rows)}")

    results: list[dict] = []
    for i, (story_key, branch, base_branch) in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {story_key} -> {branch}")
        result = resolve_story_commits(hc_all, story_key, branch, base_branch or "master")
        results.append(result)
        print(f"       linkage={result['linkage']} repo={result['repo']} n_commits={result['n_commits']}")

    resolved = sum(1 for r in results if r["linkage"] != "not_found")
    not_found = [
        {"story_key": r["story_key"], "branch": r["branch"]}
        for r in results
        if r["linkage"] == "not_found"
    ]

    summary = {
        "total_rows": len(raw_rows),
        "total_unique": len(rows),
        "resolved": resolved,
        "coverage_pct": round(100.0 * resolved / len(rows), 1) if rows else 0,
        "not_found": not_found,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"summary": summary, "stories": results}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    print("\n=== summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
