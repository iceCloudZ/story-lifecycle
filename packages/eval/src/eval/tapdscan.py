"""B 源采集 — TAPD story 清单（通过 hccli 调 TAPD API）。

用法: ``eval tapd-scan``（内部用 hccli,Windows 需 PYTHONIOENCODING=utf-8）。

- 拉 workspace ``44381896`` 的 stories:resolved|closed（量可控则连 in_progress）,
  分页拉全量,结果落 ``dataset/tapd_stories.jsonl`` 缓存（避免重复打 API）。
- 试 ``get-commit-msg``:若 TAPD 侧记录了 story 关联代码提交 → A↔B 官方关联种子,
  存 ``dataset/tapd_commits.jsonl``。
- 同时拉 ``description`` 字段（管线外 story 的 ConformanceScore 参照物兜底）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("eval.tapdscan")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = PACKAGE_ROOT / "dataset"
TAPD_STORIES = DATASET_DIR / "tapd_stories.jsonl"
TAPD_COMMITS = DATASET_DIR / "tapd_commits.jsonl"

HCCLI = "D:/agent-assets/skills/ys-cli/scripts/hccli.py"
WORKSPACE_ID = "44381896"
PAGE_SIZE = 100
MAX_PAGES = 200  # 防御上限(6309 resolved|closed 需 ~64 页)


def _run_hccli(args: list[str], timeout: int = 180) -> dict:
    """调 hccli 子命令并解析 JSON 结果。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, HCCLI, "tapd", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"hccli {args[0]} 失败: {r.stderr[:500] or r.stdout[:500]}")
    out = r.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def _extract_list(result: dict) -> list[dict]:
    """hccli 返回 {status, data: {stories: [...]} | [...] } 的兼容提取,解包 Story 包装。"""
    data = result.get("data") or {}
    if isinstance(data, dict):
        for key in ("stories", "list", "items"):
            if isinstance(data.get(key), list):
                return [d.get("Story", d) if isinstance(d, dict) else d for d in data[key]]
        return []
    if isinstance(data, list):
        return [d.get("Story", d) if isinstance(d, dict) else d for d in data]
    return []


def run_tapd_scan(story_filter: str = "resolved|closed", include_in_progress: bool = True) -> dict:
    """拉取 TAPD stories 分页落盘;已缓存的不重复拉（--force 可重拉）。"""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    statuses = [story_filter]
    if include_in_progress:
        statuses.append("in_progress")

    total = 0
    existing_ids: set[str] = set()
    if TAPD_STORIES.exists():
        for line in TAPD_STORIES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing_ids.add(json.loads(line).get("tapd_id", ""))

    with open(TAPD_STORIES, "a", encoding="utf-8") as f:
        for status in statuses:
            for page in range(1, MAX_PAGES + 1):
                params = {
                    "entity_type": "stories",
                    "status": status,
                    "page": str(page),
                    "limit": str(PAGE_SIZE),
                }
                result = _run_hccli(
                    ["get-stories", "--workspace-id", WORKSPACE_ID, "--params", json.dumps(params)]
                )
                stories = _extract_list(result)
                if not stories:
                    break  # 翻页到头
                new = 0
                for s in stories:
                    tid = str(s.get("id") or s.get("story_id") or "")
                    if not tid or tid in existing_ids:
                        continue
                    rec = {
                        "tapd_id": tid,
                        "name": s.get("name", ""),
                        "status": s.get("status", ""),
                        "iteration_id": str(s.get("iteration_id") or ""),
                        "owner": s.get("owner", ""),
                        "created": s.get("created", ""),
                        "modified": s.get("modified", ""),
                        "description": s.get("description", ""),
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    existing_ids.add(tid)
                    new += 1
                total += new
                log.info("status=%s page=%d 拉到 %d（新增 %d）", status, page, len(stories), new)
                if len(stories) < PAGE_SIZE:
                    break

    # ★ get-commit-msg 官方关联种子（尽力而为;失败不阻塞）
    commits_total = 0
    try:
        for tid in list(existing_ids)[:200]:
            try:
                res = _run_hccli(
                    ["get-commit-msg", "--workspace-id", WORKSPACE_ID, "--params", json.dumps({"story_id": tid})]
                )
            except RuntimeError as e:
                log.debug("get-commit-msg %s 失败: %s", tid, e)
                continue
            if res:
                commits_total += _append_commit_seeds(tid, res)
    except Exception as e:  # noqa: BLE001
        log.warning("get-commit-msg 批量失败: %s", e)

    log.info("TAPD stories 累计 %d;commit 种子 %d", len(existing_ids), commits_total)
    return {"stories": len(existing_ids), "commit_seeds": commits_total}


def _append_commit_seeds(tapd_id: str, result: dict) -> int:
    """把 get-commit-msg 结果追加进 tapd_commits.jsonl。"""
    data = result.get("data") if isinstance(result, dict) else result
    items: list[dict] = []
    if isinstance(data, dict):
        for k in ("commit_messages", "commits", "list", "data"):
            v = data.get(k)
            if isinstance(v, list):
                items = v
                break
    elif isinstance(data, list):
        items = data
    if not items and isinstance(result, dict):
        # 尝试整包兜底
        for k, v in result.items():
            if k != "status" and isinstance(v, (dict, list)):
                items = [v] if isinstance(v, dict) else v
                break
    n = 0
    with open(TAPD_COMMITS, "a", encoding="utf-8") as f:
        for it in items:
            rec = {"tapd_id": tapd_id}
            if isinstance(it, dict):
                rec.update(it)
            else:
                rec["raw"] = str(it)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")
    print(json.dumps(run_tapd_scan(), ensure_ascii=False))
