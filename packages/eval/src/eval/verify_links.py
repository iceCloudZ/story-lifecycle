"""独立验证 TAPD↔merge 关联（related / unrelated / uncertain）。

对两类候选做无锚定复核：
1. ``links_pending_review.md`` 中的待确认行
2. ``stories_matched.jsonl`` 中 ``link_method == "llm_mine_high"`` 的自动关联

输出 ``dataset/verify_links_<YYYYMMDD>.jsonl``，每行一条独立判断，
并生成 ``dataset/verify_links_sample_<YYYYMMDD>.md`` 供人工分层抽样校准。
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import logging
import os
import random
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import gitindex
from . import judges

log = logging.getLogger("eval.verify_links")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = PACKAGE_ROOT / "dataset"
RESULTS_DIR = PACKAGE_ROOT / "results"

REPO_ROOT = Path("D:/hc-all")
MAX_DIFF_CHARS = 120_000  # ≈ 30k token
MAX_FILES = 30


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _date_guard_verdict(
    merged_at: str, tapd_created: str, max_gap_days: int = 90
) -> tuple[str, str] | None:
    """确定性日期守卫（三态）。

    merge 早于 TAPD 需求创建（时间倒挂）时:
    - gap > max_gap_days → ("unrelated", ...) 直接 reject
    - gap ≤ max_gap_days → ("pending_guard", ...) 进待审队列，人工/下一轮 verify 定夺
    - merged_at / tapd_created 缺失或不可解析 → None（不判，避免空值误杀）

    返回 (verdict, reason) 或 None。
    """
    if not merged_at or not tapd_created:
        return None
    m = merged_at[:10]
    c = tapd_created[:10]
    if len(m) < 10 or len(c) < 10:
        return None
    try:
        from datetime import date as _date

        md = _date.fromisoformat(m)
        cd = _date.fromisoformat(c)
    except ValueError:
        return None
    gap = (cd - md).days
    if gap <= 0:
        return None
    if gap > max_gap_days:
        return (
            "unrelated",
            f"merge 早于需求创建 {gap} 天（{m} < {c}），超过 {max_gap_days} 天，确定性 reject",
        )
    return (
        "pending_guard",
        f"时间倒挂待审（merge {m} 早于需求创建 {c}，差 {gap} 天，≤{max_gap_days} 天）",
    )


def _parse_pending(path: Path) -> list[dict[str, Any]]:
    """解析 links_pending_review.md 表格行。"""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        if cells[0] == "repo" or set(cells[0]) == {"-"}:
            continue
        repo, merge, branch, merged_at, tapd_id, reason, decision = cells[:7]
        rows.append({
            "repo": repo,
            "merge_hash": merge,
            "branch": branch,
            "merged_at": merged_at,
            "tapd_id": tapd_id,
            "reason": reason,
            "source": "pending_review",
        })
    return rows


def _load_llm_mine_high(path: Path) -> list[dict[str, Any]]:
    """从 stories_matched.jsonl 抽出 llm_mine_high 关联。"""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ent = json.loads(line)
        tapd_id = ent.get("tapd_id") or ""
        for dl in ent.get("deliveries", []):
            if dl.get("link_method") == "llm_mine_high":
                rows.append({
                    "repo": dl["repo"],
                    "merge_hash": dl["merge_hash"],
                    "branch": dl.get("branch", ""),
                    "merged_at": dl.get("merged_at", ""),
                    "tapd_id": tapd_id,
                    "reason": "llm_mine_high 自动关联",
                    "source": "llm_mine_high",
                })
    return rows


def _load_tapd_stories(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        out[rec["tapd_id"]] = rec
    return out


# ---------------------------------------------------------------------------
# diff 收集（受限预算）
# ---------------------------------------------------------------------------


def _repo_path(repo_name: str) -> Path | None:
    if repo_name == "hc-admin":
        p = REPO_ROOT / "frontends" / "hc-admin"
    else:
        p = REPO_ROOT / repo_name
    return p if p.is_dir() else None


def _git(repo: Path, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _diff_text(repo_name: str, merge_hash: str, max_chars: int = MAX_DIFF_CHARS) -> tuple[str, bool]:
    """按 churn 取 top 文件 diff，总量限制在 max_chars 以内。"""
    repo = _repo_path(repo_name)
    if repo is None:
        return "", True
    base = f"{merge_hash}^1"
    r = _git(repo, ["diff", "--numstat", "-M", base, merge_hash], timeout=120)
    if r.returncode != 0:
        return "", True

    churn: list[tuple[int, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins, dele, path = parts[0], parts[1], parts[2]
        if ins == "-" or dele == "-":
            continue
        try:
            churn.append((int(ins) + int(dele), path))
        except ValueError:
            continue
    churn.sort(reverse=True)

    stat = ""
    r2 = _git(repo, ["diff", "--shortstat", base, merge_hash], timeout=120)
    if r2.returncode == 0 and r2.stdout.strip():
        stat = r2.stdout.strip()

    text = stat + "\n"
    used = len(text)
    truncated = len(churn) > MAX_FILES
    for _, path in churn[:MAX_FILES]:
        r3 = _git(repo, ["diff", "-U3", base, merge_hash, "--", path], timeout=180)
        if r3.returncode != 0:
            continue
        chunk = r3.stdout
        if used + len(chunk) > max_chars:
            truncated = True
            remaining = max_chars - used
            if remaining > 100:
                text += chunk[:remaining]
                text += "\n...（diff 已截断）\n"
            break
        text += chunk
        used += len(chunk)
    return text, truncated


# ---------------------------------------------------------------------------
# LLM 验证
# ---------------------------------------------------------------------------


class VerifyOut(BaseModel):
    verdict: str = Field(description="related / unrelated / uncertain")
    reason: str = Field(description="一句话理由")


def _build_prompt(tapd: dict, delivery: dict, diff_text: str) -> str:
    name = (tapd.get("name") or "")[:200]
    desc = _strip_html(tapd.get("description") or "")[:1_500]
    # link-only 需求:story_refs 富化正文优先（无富化时退回首段描述）
    from .ref_fetch import is_link_only, reference_for_tapd

    if is_link_only(tapd.get("description") or ""):
        ref_text, _ref_type = reference_for_tapd({tapd.get("tapd_id", ""): tapd}, tapd.get("tapd_id", ""))
        desc = ref_text[:1_500] if ref_text else desc
    branch = delivery.get("branch") or ""
    merged_at = delivery.get("merged_at") or ""
    subjects = "; ".join(
        c.get("subject", "") for c in delivery.get("commits", [])[:20]
    )
    diff = diff_text[:MAX_DIFF_CHARS]

    return (
        "你是一名中立的代码-需求关联审核员。请根据以下信息，判断代码交付 merge 是否与 TAPD 需求相关。\n\n"
        f"【TAPD 需求】\nID: {tapd.get('tapd_id', '')}\n标题: {name}\n描述: {desc}\n\n"
        f"【代码交付】\n仓库: {delivery['repo']}\n分支: {branch}\n合并时间: {merged_at}\n"
        f"commit 摘要:\n{subjects}\n\n"
        f"关键 diff（截断）:\n{diff}\n\n"
        "请只输出 JSON 对象:{\"verdict\": \"related|unrelated|uncertain\", \"reason\": \"一句话理由\"}\n"
        "- related: merge 明确实现了该 TAPD 需求的核心内容。\n"
        "- unrelated: merge 与该 TAPD 需求完全无关或实现的是另一件事。\n"
        "- uncertain: 信息不足，无法判断。\n\n"
        "禁止输出任何分析、解释或思考过程；第一个字符必须是 {，最后一个字符必须是 }。"
    )


def _verify_one(
    item: dict,
    tapd_stories: dict[str, dict],
    deliveries_index: dict[tuple[str, str], dict],
    human_confirmed_keys: set[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    key = (item["repo"], item["merge_hash"])
    delivery = deliveries_index.get(key)
    if delivery is None:
        return {**item, "verdict": "uncertain", "reason": "未在 deliveries.jsonl 中找到该 merge"}

    tapd = tapd_stories.get(item["tapd_id"], {})
    if not tapd:
        return {**item, "verdict": "uncertain", "reason": "未在 tapd_stories.jsonl 中找到该 TAPD"}

    # 优先级铁律：human_confirmed 的链接跳过一切自动规则（含日期守卫）
    merged_at = delivery.get("merged_at") or item.get("merged_at") or ""
    tapd_created = tapd.get("created") or ""
    human_confirmed = bool(
        human_confirmed_keys
        and (
            (item["repo"], item["merge_hash"], item["tapd_id"]) in human_confirmed_keys
            or (item["repo"], item["merge_hash"][:10], item["tapd_id"]) in human_confirmed_keys
        )
    )
    if not human_confirmed:
        guard = _date_guard_verdict(merged_at, tapd_created)
        if guard:
            verdict, reason = guard
            # 时间倒挂 ≤90 天 → 进待审队列（标注），不直接 reject
            if verdict == "pending_guard":
                return {
                    **item,
                    "merged_at": merged_at,
                    "verdict": "uncertain",
                    "reason": reason,
                    "guard_pending": True,
                }
            return {
                **item,
                "merged_at": merged_at,
                "verdict": verdict,
                "reason": reason,
            }

    diff_text, truncated = _diff_text(item["repo"], item["merge_hash"])
    prompt = _build_prompt(tapd, delivery, diff_text)

    try:
        res = judges._LLM.invoke_structured(prompt, VerifyOut)
        verdict = res.verdict.strip().lower()
        if verdict not in ("related", "unrelated", "uncertain"):
            verdict = "uncertain"
        return {
            **item,
            "merged_at": merged_at,
            "verdict": verdict,
            "reason": res.reason.strip(),
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("验证失败 %s:%s: %s", item["repo"], item["merge_hash"], e)
        return {**item, "merged_at": merged_at, "verdict": "uncertain", "reason": f"LLM 调用失败: {e}"}


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for c in candidates:
        key = (c["repo"], c["merge_hash"], c["tapd_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def run_verify_links(
    dataset_dir: str | Path | None = None,
    results_dir: str | Path | None = None,
    env_file: str | Path | None = None,
    concurrency: int = 8,
    seed: int = 42,
    sample_each: int = 7,
) -> dict[str, Any]:
    """运行 verify-links 并生成抽样清单。"""
    ds_dir = Path(dataset_dir) if dataset_dir else DATASET_DIR
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    ds_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    # 加载 DeepSeek env
    env_path = Path(env_file) if env_file else None
    if env_path and env_path.exists():
        judges.configure_llm_env(env_path)
    else:
        judges.configure_llm_env()

    log.info("verify-links 使用端点: %s", os.environ.get("STORY_LLM_BASE_URL"))

    candidates = _dedupe_candidates(
        _parse_pending(ds_dir / "links_pending_review.md")
        + _load_llm_mine_high(ds_dir / "stories_matched.jsonl")
    )
    log.info("待验证关联: %d", len(candidates))

    tapd_stories = _load_tapd_stories(ds_dir / "tapd_stories.jsonl")
    deliveries = gitindex.load_deliveries()
    deliveries_index = {(d["repo"], d["merge_hash"]): d for d in deliveries}
    # 短前缀索引（links_pending_review.md 里 merge 列是 10 位短 hash）
    prefix_index: dict[tuple[str, str], dict] = {}
    for d in deliveries:
        prefix_index.setdefault((d["repo"], d["merge_hash"][:10]), d)

    # 把短前缀解析回全 hash
    for c in candidates:
        key = (c["repo"], c["merge_hash"])
        if key not in deliveries_index:
            full = prefix_index.get((c["repo"], c["merge_hash"][:10]))
            if full is not None:
                c["merge_hash"] = full["merge_hash"]

    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    # human_confirmed 键（优先级铁律：跳过一切自动规则）
    from .linker import load_stories_matched

    human_confirmed_keys: set[tuple[str, str, str]] = set()
    for ent in load_stories_matched():
        for dl in ent.get("deliveries", []):
            if dl.get("human_confirmed"):
                human_confirmed_keys.add((dl["repo"], dl["merge_hash"], ent.get("tapd_id", "")))

    def _worker(item: dict) -> dict[str, Any]:
        return _verify_one(item, tapd_stories, deliveries_index, human_confirmed_keys)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_worker, c): c for c in candidates}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            rec = future.result()
            with lock:
                results.append(rec)
            if i % 50 == 0 or i == len(candidates):
                log.info("verify 进度 %d/%d", i, len(candidates))

    date = _dt.date.today().strftime("%Y%m%d")
    out_path = ds_dir / f"verify_links_{date}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.info("验证结果: %s", out_path)

    # 分层抽样
    rng = random.Random(seed)
    by_verdict: dict[str, list[dict]] = {"related": [], "unrelated": [], "uncertain": []}
    for r in results:
        by_verdict.setdefault(r["verdict"], []).append(r)
    sample: list[dict] = []
    for verdict, rows in by_verdict.items():
        n = min(sample_each, len(rows))
        sample.extend(rng.sample(rows, n) if n else [])

    sample_path = ds_dir / f"verify_links_sample_{date}.md"
    sample_path.write_text(_render_sample_md(sample, by_verdict, results), encoding="utf-8")
    log.info("抽样清单: %s", sample_path)

    return {
        "total": len(results),
        "by_verdict": {k: len(v) for k, v in by_verdict.items()},
        "sample": str(sample_path),
        "output": str(out_path),
    }


def _render_sample_md(sample: list[dict], by_verdict: dict, all_results: list[dict]) -> str:
    lines = [
        "# verify-links 分层抽样清单（人工校准）",
        "",
        f"- 总验证数: {len(all_results)}",
        f"- related: {len(by_verdict.get('related', []))}",
        f"- unrelated: {len(by_verdict.get('unrelated', []))}",
        f"- uncertain: {len(by_verdict.get('uncertain', []))}",
        "",
        "## 校准方式",
        "",
        "直接修改下方表格的 `校准` 列：",
        "- `related` / `unrelated` / `uncertain`（可覆盖模型判断）",
        "- 或整行删除表示跳过",
        "",
        "校准后保存本文件，再运行 `eval apply-verify <本文件>` 执行分级。",
        "",
        "| 来源 | repo | merge | branch | merged_at | tapd_id | 模型 verdict | 模型理由 | 校准 |",
        "|------|------|-------|--------|-----------|---------|--------------|----------|------|",
    ]
    for r in sample:
        lines.append(
            f"| {r['source']} | {r['repo']} | {r['merge_hash']} | {r.get('branch', '')} | "
            f"{r.get('merged_at', '')} | {r['tapd_id']} | {r['verdict']} | {r.get('reason', '')[:80]} |  |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 执行分级（人工校准后）
# ---------------------------------------------------------------------------


def _parse_calibrated_sample(path: Path) -> dict[tuple[str, str, str], str]:
    """解析人工校准后的抽样清单 → {(repo, merge_hash, tapd_id): 校准 verdict}。"""
    out: dict[tuple[str, str, str], str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9:
            continue
        if cells[0] == "repo" or set(cells[0]) == {"-"}:
            continue
        source, repo, merge, branch, merged_at, tapd, verdict, reason, calibrated = cells[:9]
        calibrated = calibrated.strip()
        if calibrated and calibrated in ("related", "unrelated", "uncertain"):
            out[(repo, merge, tapd)] = calibrated
    return out


def backfill_human_confirmed(
    sample_path: str | Path,
    dataset_dir: str | Path | None = None,
) -> dict[str, Any]:
    """把人工校准过的 (merge, tapd) 对回写到 stories_matched.jsonl 的 deliveries。

    标记 ``human_confirmed: true``，供 scanall 的疑似错链判定跳过（人工确认的链接
    不再被当作疑似错链追加进 pending 队列）。
    """
    from .linker import load_stories_matched

    ds_dir = Path(dataset_dir) if dataset_dir else DATASET_DIR
    confirmed = _parse_calibrated_keys(Path(sample_path))
    matched_path = ds_dir / "stories_matched.jsonl"
    entities = load_stories_matched()

    # 归一化：sample 里的 merge_hash 可能是全 hash 或 10 位前缀
    confirmed_normalized: set[tuple[str, str, str]] = set()
    for repo, mh, tapd in confirmed:
        confirmed_normalized.add((repo, mh, tapd))
        if len(mh) >= 10:
            confirmed_normalized.add((repo, mh[:10], tapd))

    marked = 0
    for ent in entities:
        for dl in ent.get("deliveries", []):
            mh = dl.get("merge_hash", "")
            if (dl["repo"], mh, ent.get("tapd_id", "")) in confirmed_normalized or (
                dl["repo"], mh[:10], ent.get("tapd_id", "")
            ) in confirmed_normalized:
                if not dl.get("human_confirmed"):
                    dl["human_confirmed"] = True
                    marked += 1

    with matched_path.open("w", encoding="utf-8") as f:
        for e in entities:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    log.info("human_confirmed 回写: %d 条 deliveries 标记", marked)
    return {"marked": marked, "sample_keys": len(confirmed), "stories_matched": str(matched_path)}


def _parse_calibrated_keys(path: Path) -> set[tuple[str, str, str]]:
    """解析人工校准后的抽样清单 → 已人工确认的 (repo, merge_hash, tapd_id) 集合。"""
    return set(_parse_calibrated_sample(path).keys())


def _load_verify_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_pending_md_for(pending: list[dict], path: Path) -> None:
    """按 _parse_pending 同构格式写回待确认队列（保留附录）。"""
    lines = [
        "# 待确认链接队列（人工标注后跑 `eval review-apply`）",
        "",
        "标注方法:在每行 `|` 分隔的表格里,把「决策」列填为 `accept:1144381896001065570`",
        "（接受某候选）/ `reject`（不关联）;保存后跑 review-apply。",
        "",
        "| repo | merge | branch | merged_at | 候选 TAPD | 理由 | 决策 |",
        "|------|-------|--------|-----------|-----------|------|------|",
    ]
    for p in pending:
        lines.append(
            f"| {p['repo']} | {p['merge_hash'][:10]} | {p.get('branch', '')} | "
            f"{p.get('merged_at', '')[:10]} | {p['tapd_id']} | {p.get('reason', '')} |  |"
        )
    tail: list[str] = []
    if path.exists():
        old = path.read_text(encoding="utf-8").splitlines()
        in_tail = False
        for ln in old:
            if ln.startswith("## ") or ln.startswith("### "):
                in_tail = True
            if in_tail:
                tail.append(ln)
    if tail:
        lines += ["", *tail]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_apply_verify(
    sample_path: str | Path,
    dataset_dir: str | Path | None = None,
    verify_path: str | Path | None = None,
) -> dict[str, Any]:
    """按人工校准结果执行分级：related→accept、unrelated→reject、uncertain→留队列。

    - related: llm_mine_high 保留为 high（method 改 verify_related）；pending 行加入
      stories_matched（同 verify_related）
    - unrelated: llm_mine_high 从 stories_matched 移除；pending 行移除
    - uncertain: llm_mine_high 降级回待确认队列；pending 行保留在队列
    """
    ds_dir = Path(dataset_dir) if dataset_dir else DATASET_DIR

    # 1. 读全部 verify 结果 + 校准覆盖
    if verify_path is None:
        dates = sorted(ds_dir.glob("verify_links_*.jsonl"))
        if not dates:
            raise RuntimeError("没有找到 verify_links_*.jsonl，先跑 `eval verify-links`")
        verify_path = dates[-1]
    rows = _load_verify_rows(Path(verify_path))
    calibrations = _parse_calibrated_sample(Path(sample_path))

    # 校准 + 日期守卫（三态）。优先级铁律：human_confirmed 跳过一切自动规则。
    from .linker import load_stories_matched

    matched_path = ds_dir / "stories_matched.jsonl"
    pending_path = ds_dir / "links_pending_review.md"
    entities = load_stories_matched()
    human_confirmed_keys: set[tuple[str, str, str]] = set()
    for ent in entities:
        for dl in ent.get("deliveries", []):
            if dl.get("human_confirmed"):
                human_confirmed_keys.add((dl["repo"], dl["merge_hash"], ent.get("tapd_id", "")))

    tapd_stories = _load_tapd_stories(ds_dir / "tapd_stories.jsonl")
    deliveries_for_guard = gitindex.load_deliveries()
    deliveries_guard_index = {(d["repo"], d["merge_hash"]): d for d in deliveries_for_guard}
    guard_rejected: list[dict] = []
    guard_pending: list[dict] = []
    verdict_map: dict[tuple[str, str, str], str] = {}
    reason_map: dict[tuple[str, str, str], str] = {}
    for r in rows:
        key = (r["repo"], r["merge_hash"], r["tapd_id"])
        # human_confirmed 铁律：跳过一切自动规则（含日期守卫、疑似错链判定）
        if key in human_confirmed_keys or (
            r["repo"], r["merge_hash"][:10], r["tapd_id"]
        ) in human_confirmed_keys:
            verdict_map[key] = calibrations.get(key, r.get("verdict", "related"))
            continue
        merged_at = r.get("merged_at") or ""
        if not merged_at:
            dl = deliveries_guard_index.get((r["repo"], r["merge_hash"]))
            merged_at = (dl or {}).get("merged_at") or ""
        tapd_created = (tapd_stories.get(r["tapd_id"]) or {}).get("created") or ""
        guard = _date_guard_verdict(merged_at, tapd_created)
        if guard:
            verdict, reason = guard
            reason_map[key] = reason
            if verdict == "unrelated":
                verdict_map[key] = "unrelated"
                guard_rejected.append({"repo": r["repo"], "merge_hash": r["merge_hash"], "tapd_id": r["tapd_id"]})
            else:
                # 时间倒挂 ≤90 天 → 进待审队列
                verdict_map[key] = "uncertain"
                guard_pending.append({
                    "repo": r["repo"],
                    "merge_hash": r["merge_hash"],
                    "tapd_id": r["tapd_id"],
                    "reason": reason,
                })
        else:
            verdict_map[key] = calibrations.get(key, r.get("verdict", "uncertain"))

    deliveries_by_key = gitindex.load_deliveries()
    deliveries_index = {(d["repo"], d["merge_hash"]): d for d in deliveries_by_key}
    prefix_index: dict[tuple[str, str], dict] = {}
    for d in deliveries_by_key:
        prefix_index.setdefault((d["repo"], d["merge_hash"][:10]), d)

    def _resolve_merge(repo: str, mhash: str) -> str:
        if (repo, mhash) in deliveries_index:
            return mhash
        full = prefix_index.get((repo, mhash[:10]))
        return full["merge_hash"] if full else mhash

    accepted: list[dict] = []
    rejected: list[dict] = []
    kept_uncertain: list[dict] = []

    # 3. 处理 stories_matched 中的 llm_mine_high
    entities_by_tapd: dict[str, dict] = {e["tapd_id"]: e for e in entities if e.get("tapd_id")}
    for ent in entities:
        new_deliveries: list[dict] = []
        for dl in ent.get("deliveries", []):
            if dl.get("link_method") != "llm_mine_high":
                new_deliveries.append(dl)
                continue
            key = (dl["repo"], dl["merge_hash"], ent["tapd_id"])
            verdict = verdict_map.get(key, "uncertain")
            if verdict == "related":
                dl["link_method"] = "verify_related"
                dl["confidence"] = "high"
                dl["verify_reason"] = "verified_related"
                new_deliveries.append(dl)
                accepted.append({"repo": dl["repo"], "merge_hash": dl["merge_hash"], "tapd_id": ent["tapd_id"]})
            elif verdict == "unrelated":
                rejected.append({
                    "repo": dl["repo"],
                    "merge_hash": dl["merge_hash"],
                    "tapd_id": ent["tapd_id"],
                    "reason": reason_map.get(key, "verify unrelated"),
                })
            else:
                # uncertain → 降级回待确认队列
                kept_uncertain.append({
                    "repo": dl["repo"],
                    "merge_hash": dl["merge_hash"],
                    "branch": dl.get("branch", ""),
                    "merged_at": dl.get("merged_at", ""),
                    "tapd_id": ent["tapd_id"],
                    "reason": "verify uncertain（降级自 llm_mine_high）",
                })
        ent["deliveries"] = new_deliveries
        if not new_deliveries:
            ent["link_summary"]["A_B"] = ""

    # 4. 处理 pending 行
    for p in pending_rows:
        full_hash = _resolve_merge(p["repo"], p["merge_hash"])
        key = (p["repo"], full_hash, p["tapd_id"])
        verdict = verdict_map.get(key, "uncertain")
        if verdict == "related":
            # 加入 stories_matched
            ent = entities_by_tapd.get(p["tapd_id"])
            if ent is None:
                ent = {
                    "tapd_id": p["tapd_id"],
                    "name": "",
                    "status": "",
                    "iteration_id": "",
                    "owner": "",
                    "story_key": "",
                    "story_title": "",
                    "evidence_dir": "",
                    "deliveries": [],
                    "link_summary": {"A_B": "", "A_C": "", "B_C": ""},
                    "link_notes": ["verified_related"],
                }
                entities_by_tapd[p["tapd_id"]] = ent
                entities.append(ent)
            dl = {
                "repo": p["repo"],
                "merge_hash": full_hash,
                "branch": p.get("branch", ""),
                "link_method": "verify_related",
                "confidence": "high",
            }
            if not any(d["repo"] == dl["repo"] and d["merge_hash"] == dl["merge_hash"] for d in ent["deliveries"]):
                ent["deliveries"].append(dl)
            ent["link_summary"]["A_B"] = "high"
            accepted.append({"repo": p["repo"], "merge_hash": full_hash, "tapd_id": p["tapd_id"]})
        elif verdict == "unrelated":
            rejected.append({
                "repo": p["repo"],
                "merge_hash": full_hash,
                "tapd_id": p["tapd_id"],
                "reason": reason_map.get(key, "verify unrelated"),
            })
        else:
            kept_uncertain.append({
                "repo": p["repo"],
                "merge_hash": full_hash,
                "branch": p.get("branch", ""),
                "merged_at": p.get("merged_at", ""),
                "tapd_id": p["tapd_id"],
                "reason": reason_map.get(key, p.get("reason", "verify uncertain")),
            })

    # 5. 写回 stories_matched.jsonl
    entities = [e for e in entities if e.get("deliveries") or e.get("story_key")]
    linked_before = sum(
        1
        for e in entities
        for d in e.get("deliveries", [])
        if d.get("confidence") in ("high", "official", "confirmed")
    )
    with matched_path.open("w", encoding="utf-8") as f:
        for e in entities:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    linked_after = sum(
        1
        for e in entities
        for d in e.get("deliveries", [])
        if d.get("confidence") in ("high", "official", "confirmed")
    )

    # 6. 写回 pending 队列（保留 uncertain + 时间倒挂待审）
    kept_uncertain.extend(guard_pending)
    kept_uncertain.sort(key=lambda x: (x["repo"], x["merge_hash"]))
    _write_pending_md_for(kept_uncertain, pending_path)

    # 7. 写 verify 摘要（scanall 报告读取用）
    date = _dt.date.today().strftime("%Y%m%d")
    summary = {
        "date": date,
        "total": len(rows),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "kept_uncertain": len(kept_uncertain),
        "guard_rejected": len(guard_rejected),
        "guard_pending": len(guard_pending),
        "linked_before": linked_before,
        "linked_after": linked_after,
    }
    (ds_dir / "verify_apply_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "total": len(rows),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "kept_uncertain": len(kept_uncertain),
        "guard_rejected": len(guard_rejected),
        "guard_pending": len(guard_pending),
        "linked_before": linked_before,
        "linked_after": linked_after,
        "accepted_sample": accepted[:5],
        "stories_matched": str(matched_path),
        "pending_review": str(pending_path),
    }
