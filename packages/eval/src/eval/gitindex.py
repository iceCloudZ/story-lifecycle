"""A 源采集 — git merge 交付单元索引（只读）。

扫 ``D:/hc-all/*/`` 与 ``D:/hc-all/frontends/hc-admin`` 下含 ``.git`` 的仓库
（**不 fetch**,只用本地 ``origin/master`` 引用）:

- ``git log origin/master --merges`` → merge 单元（branch 名从 message 正则提取）
- 每 merge:``git log <m>^1..<m>^2`` 分支内提交 + ``git diff --shortstat <m>^1 <m>``

输出 ``dataset/deliveries.jsonl``（每行一个交付单元）。
只读纪律:对 hc-all 各仓库禁止 checkout/reset/clean/fetch。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("eval.gitindex")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = PACKAGE_ROOT / "dataset"
DELIVERIES = DATASET_DIR / "deliveries.jsonl"
DONE_MARK = DATASET_DIR / "deliveries_done.json"

SCAN_ROOTS = ["D:/hc-all", "D:/hc-all/frontends"]
MERGE_MSG_RE = re.compile(r"^Merge branch '([^']+)'")
# 非标准 merge message（如 "Merge remote-tracking branch ..."）也尝试提取
ALT_MERGE_RE = re.compile(r"Merge (?:branch |remote-tracking branch )?'?([^'\"\s]+)'?")
SKIP_REPOS = {
    "hc-pytest",  # 无 origin/master 的仓库
    "hc-risk-management",  # 非 zzh 负责的团队仓库（926 merges,不属交付范围）
}


def find_repos() -> list[Path]:
    repos: list[Path] = []
    for root in SCAN_ROOTS:
        root_p = Path(root)
        if not root_p.is_dir():
            continue
        for child in sorted(root_p.iterdir()):
            if not child.is_dir():
                continue
            if (child / ".git").exists():
                repos.append(child)
    return repos


def _git(repo: Path, args: list[str], timeout: int = 300) -> str:
    r = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed in {repo.name}: {r.stderr[:300]}")
    return r.stdout


def _extract_branch(msg: str) -> str | None:
    m = MERGE_MSG_RE.match(msg or "")
    if m:
        return m.group(1)
    m = ALT_MERGE_RE.search(msg or "")
    return m.group(1) if m else None


def _parse_shortstat(text: str) -> dict:
    """解析 `git diff --shortstat` 输出 → {files, insertions, deletions}。"""
    out = {"files": 0, "insertions": 0, "deletions": 0}
    if not text.strip():
        return out
    m = re.search(r"(\d+) file", text)
    out["files"] = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) insertion", text)
    out["insertions"] = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) deletion", text)
    out["deletions"] = int(m.group(1)) if m else 0
    return out


def _build_commit_author_map(repo: Path) -> dict[str, str]:
    """建仓库全量 commit hash → author name 映射（用于 enrichment）。"""
    try:
        out = _git(repo, ["log", "--all", "--format=%H%x1f%an"])
    except RuntimeError as e:
        log.warning("%s 建 commit author 映射失败: %s", repo.name, e)
        return {}
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            continue
        cp = line.split("\x1f")
        if len(cp) >= 2:
            mapping[cp[0]] = cp[1]
    return mapping


def index_repo(repo: Path) -> list[dict]:
    """索引单仓库的全部 merge 交付单元，并给每个 commit 补上 author。"""
    has_master = (
        subprocess.run(
            ["git", "rev-parse", "--verify", "origin/master"],
            cwd=str(repo), capture_output=True, timeout=60,
        ).returncode
        == 0
    )
    if not has_master:
        log.warning("跳过 %s:无 origin/master", repo.name)
        return []

    author_map = _build_commit_author_map(repo)

    merges_raw = _git(repo, ["log", "origin/master", "--merges", "--format=%H%x1f%aI%x1f%an%x1f%s"])
    deliveries: list[dict] = []
    for line in merges_raw.splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        merge_hash, merged_at, author, msg = parts[0], parts[1], parts[2], parts[3]
        branch = _extract_branch(msg)
        # 分支内提交（m^1..m^2;octopus 取第一个非第一父）
        try:
            parents = _git(repo, ["log", "-1", "--format=%P", merge_hash]).split()
        except RuntimeError:
            parents = []
        if len(parents) < 2:
            # 理论上 --merges 不会出现;防御跳过
            log.debug("跳过 %s:%s（父数=%d）", repo.name, merge_hash, len(parents))
            continue
        base, topic = parents[0], parents[1]
        try:
            commits_raw = _git(repo, ["log", "--format=%H%x1f%aI%x1f%s", f"{base}..{topic}"])
        except RuntimeError as e:
            log.warning("%s:%s 分支内提交失败: %s", repo.name, merge_hash, e)
            commits_raw = ""
        commits = []
        for cl in commits_raw.splitlines():
            cp = cl.split("\x1f")
            if len(cp) >= 2:
                h = cp[0]
                commits.append({
                    "hash": h,
                    "date": cp[1],
                    "subject": cp[2] if len(cp) > 2 else "",
                    "author": author_map.get(h, ""),
                })
        # diffstat
        try:
            stat = _parse_shortstat(_git(repo, ["diff", "--shortstat", f"{base}", merge_hash]))
        except RuntimeError:
            stat = {}
        deliveries.append(
            {
                "repo": repo.name,
                "merge_hash": merge_hash,
                "branch": branch or "",
                "merged_at": merged_at,
                "author": author,
                "commits": commits,
                "diffstat": stat,
                "kind": "merge",
            }
        )
    log.info("%s: %d 个 merge 单元", repo.name, len(deliveries))
    return deliveries


def run_index() -> dict:
    """全量索引;已处理的仓库跳过（增量可恢复）。"""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if DONE_MARK.exists():
        done = set(json.loads(DONE_MARK.read_text(encoding="utf-8")))

    total = 0
    with open(DELIVERIES, "a", encoding="utf-8") as f:
        for repo in find_repos():
            if repo.name in done or repo.name in SKIP_REPOS:
                continue
            try:
                deliveries = index_repo(repo)
            except Exception as e:  # noqa: BLE001
                log.exception("%s 索引失败", repo.name)
                continue
            for d in deliveries:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
            f.flush()
            done.add(repo.name)
            DONE_MARK.write_text(json.dumps(sorted(done)), encoding="utf-8")
            total += len(deliveries)

    lines = len(DELIVERIES.read_text(encoding="utf-8").splitlines()) if DELIVERIES.exists() else 0
    log.info("deliveries.jsonl 累计 %d 行", lines)
    return {"repos_done": len(done), "deliveries_total": lines}


def load_deliveries() -> list[dict]:
    """读取 deliveries.jsonl。"""
    if not DELIVERIES.exists():
        return []
    out = []
    for line in DELIVERIES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def enrich_commit_authors() -> dict[str, Any]:
    """对已有 deliveries.jsonl 里的每个 commit 补 author（不重新扫 merge）。

    用于已有索引文件的增量 enrichment；新建索引会在 index_repo 里直接带上 author。
    """
    if not DELIVERIES.exists():
        return {"enriched": 0, "total": 0}

    repos = {repo.name: repo for repo in find_repos()}
    author_maps: dict[str, dict[str, str]] = {}
    for repo_name, repo in repos.items():
        author_maps[repo_name] = _build_commit_author_map(repo)
        log.info("%s: commit author 映射 %d 条", repo_name, len(author_maps[repo_name]))

    out: list[dict] = []
    total_commits = 0
    enriched = 0
    for d in load_deliveries():
        repo_name = d.get("repo", "")
        amap = author_maps.get(repo_name, {})
        for c in d.get("commits", []):
            total_commits += 1
            h = c.get("hash", "")
            if not c.get("author") and h in amap:
                c["author"] = amap[h]
                enriched += 1
        out.append(d)

    DELIVERIES.write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in out),
        encoding="utf-8",
    )
    log.info("enrichment 完成: %d/%d commits 补了 author", enriched, total_commits)
    return {"enriched": enriched, "total": total_commits}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")
    print(json.dumps(run_index(), ensure_ascii=False))
