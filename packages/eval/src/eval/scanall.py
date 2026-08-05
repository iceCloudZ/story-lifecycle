"""全量扫描评分 — 对 ``deliveries.jsonl`` 全部 merge 逐个评分（不只 core 集）。

评什么（按关联状态分档）:

- 有关联 story（high/official/已确认）: ``ConformanceScore``（参照物
  spec > PRD > TAPD 描述）+ ``DeliveryScore``
- 无关联 story: ``MergeSummary`` + ``DeliveryScore``;摘要随行落盘,
  作为第二轮模糊关联的素材（新命中的进待确认列表）

断点续跑: 结果按 ``(repo, merge_hash)`` 追加写 ``results/merge_scores.jsonl``
（每完成一个追加一行）,重跑自动跳过已完成;支持 ``--limit N`` 分批。

diff 预算: 单 merge 送审内容设上限（diffstat + 按 churn 排序的 top 文件 diff,
总量截断 ~80k token）;超限的结果标 ``truncated: true``。

进度: 每 50 个输出一次（已完成/剩余/速率/ETA）;连续失败率突增（服务端抖动）
时短暂退避后自动恢复。启动前检查 ``scan_all.lock``（同类进程在跑则中止）。
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from statistics import mean
from typing import Any

from . import dataset
from . import gitindex
from . import judges

log = logging.getLogger("eval.scanall")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PACKAGE_ROOT / "results"
DATASET_DIR = PACKAGE_ROOT / "dataset"
SCORES_PATH = RESULTS_DIR / "merge_scores.jsonl"
LOCK_PATH = RESULTS_DIR / "scan_all.lock"

DIFF_BUDGET_CHARS = 300_000  # 收集上限（judge 内部再截 120k）
MAX_FILES = 40  # 按 churn 排序参与送审的文件上限
PROGRESS_EVERY = 50
BURST_FAILURES = 5
BURST_BACKOFF_S = 120
DEFAULT_CONCURRENCY = 1  # 默认串行，保持原行为

# 中英文 author 归一：这些名字视为同一人
AUTHOR_ALIASES: dict[str, str] = {
    "赵子豪": "zhaozihao",
}
# 个人交付默认 author / 分支正则
MINE_AUTHORS = {"zhaozihao", "赵子豪"}
MINE_BRANCH_RE = re.compile(
    r"(feature/(ice|zzh)[/_]|_zzh_|^zzh[/_]|ice_)",
    re.IGNORECASE,
)


def normalize_author(author: str) -> str:
    """把中英文别名归一到统一标识（默认英文）。"""
    a = (author or "").strip()
    return AUTHOR_ALIASES.get(a, a)


def _is_mine_author(author: str) -> bool:
    return normalize_author(author) in MINE_AUTHORS


def _branch_matches_mine(branch: str) -> bool:
    return bool(MINE_BRANCH_RE.search(branch or ""))


def classify_ownership(delivery: dict) -> str:
    """判断交付单元归属：lead / participant / none。

    - lead: merge author 是我 / 分支名命中个人规则 / direct push 且我 authored 过半
    - participant: 不满足 lead，但分支提交里有我 authored 的提交
    - none: 都不是
    """
    # a) merge author
    if _is_mine_author(delivery.get("author", "")):
        return "lead"
    # b) branch name
    if _branch_matches_mine(delivery.get("branch", "")):
        return "lead"
    # c) direct push majority
    commits = delivery.get("commits", [])
    if delivery.get("kind") == "direct" and commits:
        mine_commits = sum(1 for c in commits if _is_mine_author(c.get("author", "")))
        if mine_commits > len(commits) / 2:
            return "lead"
    # participant: any commit authored by me
    if any(_is_mine_author(c.get("author", "")) for c in commits):
        return "participant"
    return "none"


def make_delivery_filter(
    authors: list[str] | None = None,
    branch_patterns: list[str] | None = None,
    mine: bool = False,
) -> callable:
    """构造 deliveries 过滤器。

    参数:
        authors: 精确匹配的 author 列表（归一后比较）
        branch_patterns: 分支名 fnmatch 通配列表（旧接口兼容）
        mine: 快捷方式，使用个人归属规则（lead + participant 都算）
    """
    target_authors: set[str] = set()
    patterns: list[str] = []
    if mine:
        return lambda d: classify_ownership(d) in ("lead", "participant")
    if authors:
        target_authors.update(normalize_author(a) for a in authors if a)
    if branch_patterns:
        patterns.extend(p for p in branch_patterns if p)

    def _filter(delivery: dict) -> bool:
        author_ok = False
        if target_authors:
            author_ok = normalize_author(delivery.get("author", "")) in target_authors
        branch_ok = False
        if patterns:
            import fnmatch
            branch = delivery.get("branch", "") or ""
            branch_ok = any(fnmatch.fnmatch(branch, p) for p in patterns)
        return author_ok or branch_ok

    return _filter


def _git(repo: Path, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _repo_path(repo_name: str) -> Path | None:
    if repo_name == "hc-admin":
        p = Path("D:/hc-all/frontends/hc-admin")
    else:
        p = Path("D:/hc-all") / repo_name
    return p if p.is_dir() else None


# ---------------------------------------------------------------------------
# 关联索引
# ---------------------------------------------------------------------------


def load_match_index() -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    """(repo, merge_hash) → {entity, confidence};tapd_id → tapd story。"""
    idx: dict[tuple[str, str], dict] = {}
    tapd: dict[str, dict] = {}
    p = DATASET_DIR / "stories_matched.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ent = json.loads(line)
            for dl in ent.get("deliveries", []):
                conf = dl.get("confidence", "")
                if conf not in ("high", "official", "confirmed"):
                    continue
                key = (dl["repo"], dl["merge_hash"])
                if key not in idx or idx[key].get("confidence") == "confirmed":
                    idx[key] = {
                        "entity": ent,
                        "confidence": conf,
                        "human_confirmed": bool(dl.get("human_confirmed")),
                    }
    tp = DATASET_DIR / "tapd_stories.jsonl"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                tapd[rec["tapd_id"]] = rec
    return idx, tapd


def _tapd_desc(tapd: dict[str, dict], tid: str) -> str:
    import re

    desc = (tapd.get(tid) or {}).get("description") or ""
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc)
    return desc.strip()[:40_000]


def _evidence_reference(entity: dict) -> tuple[str, str]:
    """证据目录里取参照物:spec > PRD。返回 (text, type)。"""
    ed = entity.get("evidence_dir") or ""
    if not ed:
        return "", ""
    d = Path(ed)
    if not d.is_dir():
        return "", ""
    for doc_key, cands in (("spec", ["spec.md", "Spec.md", "design.md"]), ("prd", ["PRD.md", "prd.md", "Prd.md"])):
        for cand in cands:
            f = d / cand
            if f.exists() and f.stat().st_size > 0:
                return dataset._read_text_robust(f), doc_key
    return "", ""


def _tapd_reference(tapd: dict, tapd_id: str) -> tuple[str, str]:
    """TAPD 参照物:link-only 且 story_refs 富化成功 → (story_refs 正文, "story_refs");
    否则 description 文本。参照物优先级: C 源 spec > C 源 PRD > story_refs > TAPD 描述。"""
    from .ref_fetch import reference_for_tapd

    return reference_for_tapd(tapd, tapd_id)


# ---------------------------------------------------------------------------
# diff 预算
# ---------------------------------------------------------------------------


def _diff_text(repo_name: str, merge_hash: str) -> tuple[str, bool]:
    """按 churn 排序取 top 文件 diff,diffstat 前缀,总量截断。返回 (text, truncated)。"""
    repo = _repo_path(repo_name)
    if repo is None:
        return "", True
    base = f"{merge_hash}^1"
    # 每文件 churn:numstat
    r = _git(repo, ["diff", "--numstat", "-M", base, merge_hash], timeout=120)
    if r.returncode != 0:
        return "", True
    churn: list[tuple[int, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins, dele = parts[0], parts[1]
        path = parts[2]
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
        if used + len(chunk) > DIFF_BUDGET_CHARS:
            truncated = True
            break
        text += chunk
        used += len(chunk)
    return text, truncated


# ---------------------------------------------------------------------------
# 单 merge 评分
# ---------------------------------------------------------------------------


def score_delivery(delivery: dict, linked: dict | None, tapd: dict[str, dict]) -> dict[str, Any]:
    """对单个 merge 打分。linked 为 load_match_index 的 {entity, confidence} 或 None。"""
    row: dict[str, Any] = {
        "repo": delivery["repo"],
        "merge_hash": delivery["merge_hash"],
        "branch": delivery.get("branch", ""),
        "merged_at": delivery.get("merged_at", ""),
        "author": delivery.get("author", ""),
        "diffstat": delivery.get("diffstat", {}),
        "truncated": False,
        "scored_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    commits = delivery.get("commits", [])
    if linked:
        ent = linked["entity"]
        row["tapd_id"] = ent.get("tapd_id") or ""
        row["story_key"] = ent.get("story_key") or ""
        row["confidence"] = linked["confidence"]
        row["human_confirmed"] = bool(linked.get("human_confirmed"))
        ref, ref_type = _evidence_reference(ent)
        if not ref:
            ref, ref_type = _tapd_reference(tapd, ent.get("tapd_id") or "")
        if ref:
            diff_text, truncated = _diff_text(delivery["repo"], delivery["merge_hash"])
            row["truncated"] = truncated
            try:
                cscore = judges.judge_conformance(ref, ref_type, diff_text)
                row["conformance_score"] = cscore.model_dump()
            except Exception as e:  # noqa: BLE001
                row["error"] = f"conformance: {e}"
                log.warning("Conformance 失败 %s:%s: %s", delivery["repo"], delivery["merge_hash"][:10], e)
        else:
            log.info("%s:%s 无参照物,只评 delivery", delivery["repo"], delivery["merge_hash"][:10])
    else:
        row["tapd_id"] = ""
        row["story_key"] = ""
        row["confidence"] = ""
        try:
            ms = judges.judge_merge_summary(commits, delivery["repo"], delivery.get("branch", ""), delivery.get("diffstat", {}))
            row["merge_summary"] = ms.model_dump()
        except Exception as e:  # noqa: BLE001
            row["error"] = f"summary: {e}"
            log.warning("MergeSummary 失败 %s:%s: %s", delivery["repo"], delivery["merge_hash"][:10], e)

    try:
        dscore = judges.judge_delivery(commits, delivery["repo"], delivery.get("branch", ""))
        row["delivery_score"] = dscore.model_dump()
    except Exception as e:  # noqa: BLE001
        row["error"] = (row.get("error") + "; " if row.get("error") else "") + f"delivery: {e}"
        log.warning("DeliveryScore 失败 %s:%s: %s", delivery["repo"], delivery["merge_hash"][:10], e)
    return row


# ---------------------------------------------------------------------------
# 断点续跑锁
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Windows 用 OpenProcess(SYNCHRONIZE) 检查进程存活。"""
    import ctypes

    PROCESS_SYNCHRONIZE = 0x00100000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_SYNCHRONIZE, 0, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def acquire_lock() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            rec = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            pid = int(rec.get("pid", 0))
            if pid and _pid_alive(pid):
                raise RuntimeError(
                    f"scan-all 已在运行(pid={pid},{rec.get('started','')})——请勿重复启动"
                )
        except (json.JSONDecodeError, OSError):
            pass
        log.warning("发现过期 lock,覆盖")
    LOCK_PATH.write_text(
        json.dumps({"pid": __import__("os").getpid(), "started": _dt.datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8",
    )


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_scan_all(
    limit: int | None = None,
    results_dir: str | Path | None = None,
    max_attempts: int = 3,
    *,
    authors: list[str] | None = None,
    branch_patterns: list[str] | None = None,
    mine: bool = False,
) -> dict[str, Any]:
    res_dir = Path(results_dir) if results_dir else RESULTS_DIR
    res_dir.mkdir(parents=True, exist_ok=True)
    delivery_filter = make_delivery_filter(authors=authors, branch_patterns=branch_patterns, mine=mine)
    report_suffix = "_mine" if mine else ""
    acquire_lock()
    try:
        return _run(res_dir, limit, max_attempts, delivery_filter=delivery_filter, report_suffix=report_suffix)
    finally:
        release_lock()


def _load_rows(path: Path) -> dict[tuple[str, str], dict]:
    """读 merge_scores.jsonl,(repo, merge_hash) 去重、同键后写覆盖。"""
    rows: dict[tuple[str, str], dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                rows[(rec.get("repo", ""), rec.get("merge_hash", ""))] = rec
    return rows


def _run(
    res_dir: Path,
    limit: int | None,
    max_attempts: int,
    delivery_filter: callable | None = None,
    report_suffix: str = "",
) -> dict[str, Any]:
    deliveries = gitindex.load_deliveries()
    if not deliveries:
        raise RuntimeError("deliveries.jsonl 为空——先跑 `eval index`")
    # 按 (repo, merge_hash) 去重（保留最后一条）
    seen: dict[tuple[str, str], dict] = {}
    for d in deliveries:
        seen[(d["repo"], d["merge_hash"])] = d

    if delivery_filter:
        ordered = [d for d in seen.values() if delivery_filter(d)]
        log.info("过滤器命中 %d / %d 个 merge", len(ordered), len(seen))
    else:
        ordered = list(seen.values())

    if limit:
        ordered = ordered[:limit]

    rows = _load_rows(SCORES_PATH)
    # 有 error 的行视为未完成——断点续跑时重试,不硬跳过
    done: set[tuple[str, str]] = {
        key for key, rec in rows.items() if not rec.get("error")
    }

    idx, tapd = load_match_index()
    ordered_keys = [(d["repo"], d["merge_hash"]) for d in ordered]
    todo = [(repo, h) for repo, h in ordered_keys if (repo, h) not in done]
    already_done_in_ordered = len(ordered_keys) - len(todo)
    log.info(
        "scan-all: 共 %d 个 merge,本批已完成 %d,待评 %d",
        len(ordered), already_done_in_ordered, len(todo),
    )

    concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", str(DEFAULT_CONCURRENCY)))
    if concurrency < 1:
        concurrency = 1
    log.info("并发数: %d", concurrency)

    # 重置 judge token 计数器
    judges.reset_token_usage()

    errors: list[str] = []
    failures_in_row = 0
    t0 = time.monotonic()
    written = 0

    def _score_one(args):
        i, repo, h = args
        d = seen[(repo, h)]
        linked = idx.get((repo, h))
        try:
            row = score_delivery(d, linked, tapd)
        except Exception as e:  # noqa: BLE001
            row = {
                "repo": repo, "merge_hash": h, "branch": d.get("branch", ""),
                "merged_at": d.get("merged_at", ""), "author": d.get("author", ""),
                "error": str(e), "scored_at": _dt.datetime.now().isoformat(timespec="seconds"),
            }
        row.setdefault("ownership", classify_ownership(d))
        return i, repo, h, row

    def _handle_row(repo: str, h: str, row: dict) -> None:
        nonlocal failures_in_row
        key = f"{repo}:{h[:10]}"
        if row.get("error"):
            errors.append(f"{key}: {row['error']}")
            log.warning("打分失败 %s: %s", key, row['error'])
            failures_in_row += 1
        else:
            failures_in_row = 0

    def _log_progress() -> None:
        elapsed = time.monotonic() - t0
        rate = written / max(elapsed, 1e-6)
        eta = (len(todo) - written) / max(rate, 1e-6)
        log.info(
            "scan-all 进度 %d/%d,速率 %.2f/s,ETA %.0fmin,失败 %d",
            written, len(todo), rate, eta / 60, len(errors),
        )

    with open(SCORES_PATH, "a", encoding="utf-8") as f:
        if concurrency == 1:
            for i, (repo, h) in enumerate(todo, 1):
                _, repo, h, row = _score_one((i, repo, h))
                _handle_row(repo, h, row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                written += 1

                if written % PROGRESS_EVERY == 0 or written == len(todo):
                    _log_progress()
        else:
            lock = threading.Lock()
            paused_until = [0.0]

            def _worker(args):
                # 全局暂停：服务端抖动时让新任务等待
                with lock:
                    wait = paused_until[0] - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                return _score_one(args)

            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(_worker, (i, repo, h)): (i, repo, h)
                    for i, (repo, h) in enumerate(todo, 1)
                }
                for future in concurrent.futures.as_completed(futures):
                    i, repo, h, row = future.result()
                    _handle_row(repo, h, row)
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    written += 1

                    if written % PROGRESS_EVERY == 0 or written == len(todo):
                        _log_progress()

                    # 触发退避后通知其它 worker 暂停
                    if failures_in_row >= BURST_FAILURES:
                        with lock:
                            paused_until[0] = time.monotonic() + BURST_BACKOFF_S
                        log.warning("设置全局退避 %ds", BURST_BACKOFF_S)
                        time.sleep(BURST_BACKOFF_S)
                        failures_in_row = 0

    token_usage = judges.get_token_usage()
    report = _render_report(
        res_dir, ordered, tapd, idx, token_usage,
        delivery_filter=delivery_filter, report_suffix=report_suffix,
    )
    date = _dt.date.today().strftime("%Y%m%d")
    md_path = res_dir / f"full_scan{report_suffix}_{date}.md"
    md_path.write_text(report, encoding="utf-8")
    log.info("全量扫描报告: %s", md_path)
    return {
        "total": len(ordered),
        "scored_now": written,
        "already_done": len(done),
        "errors": errors,
        "report": str(md_path),
        "token_usage": token_usage,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _dims(row: dict) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    conf = row.get("conformance_score") or {}
    for d in ("alignment", "coverage", "scope_drift"):
        if d in conf:
            out.append((f"conf.{d}", conf[d]))
    dv = row.get("delivery_score") or {}
    for d in ("message_quality", "granularity", "rework"):
        if d in dv:
            out.append((f"delivery.{d}", dv[d]))
    return out


def _month_key(merged_at: str) -> str:
    """从 ISO 时间字符串取 YYYY-MM。"""
    try:
        dt = _dt.datetime.fromisoformat(merged_at)
        return dt.strftime("%Y-%m")
    except Exception:
        return "unknown"


def _drift_severity(row: dict) -> int:
    """低分严重程度：alignment 越低越严重，coverage/scope 作为辅助。"""
    conf = row.get("conformance_score") or {}
    return (
        conf.get("alignment", 5) * 100
        + conf.get("coverage", 5)
        + (5 - conf.get("scope_drift", 5))
    )


_SUSPECTED_WRONG_LINK_KEYWORDS = (
    "完全无关",
    "未发现任何相关实现",
    "完全不符",
    "完全脱节",
    "完全偏离",
    "完全不是",
    "完全未实现",
    "完全未覆盖",
    "严重不符",
    "完全没有任何相关",
)


def _is_suspected_wrong_link(row: dict) -> bool:
    """alignment=1 且 judge 明确说实现与需求完全无关 → 大概率是 merge↔story 错链。

    人工已确认（human_confirmed）的链接跳过——人工校准是最高权威，不再进疑似错链。
    """
    if row.get("human_confirmed"):
        return False
    cf = row.get("conformance_score") or {}
    if cf.get("alignment") != 1:
        return False
    text = f"{cf.get('summary', '')} {' '.join(cf.get('findings') or [])}"
    return any(kw in text for kw in _SUSPECTED_WRONG_LINK_KEYWORDS)


def _append_suspected_to_pending(suspected: list[dict], path: Path) -> None:
    """把疑似错链追加到 links_pending_review.md，等待人工确认/拒绝。"""
    if not suspected:
        return
    if path.exists():
        old = path.read_text(encoding="utf-8").splitlines()
        # 保留头部表格，直到第一个附录小节
        header_end = 0
        in_tail = False
        for i, ln in enumerate(old):
            if ln.startswith("## ") or ln.startswith("### "):
                in_tail = True
            if not in_tail:
                header_end = i + 1
        head = old[:header_end]
        tail = old[header_end:]
        existing = {
            (parts[1].strip(), parts[2].strip())
            for ln in head
            if ln.startswith("| ")
            for parts in [ln.split("|")]
            if len(parts) >= 4 and parts[1].strip() != "repo" and set(parts[1].strip()) != {"-"}
        }
    else:
        head = [
            "# 待确认链接队列（人工标注后跑 `eval review-apply`）",
            "",
            "标注方法:在每行 `|` 分隔的表格里,把「决策」列填为 `accept:1144381896001065570`",
            "（接受某候选）/ `reject`（不关联）;保存后跑 review-apply。",
            "",
            "| repo | merge | branch | merged_at | 候选 TAPD | 理由 | 决策 |",
            "|------|-------|--------|-----------|-----------|------|------|",
        ]
        tail = []
        existing = set()

    new_lines: list[str] = []
    for r in sorted(suspected, key=lambda x: (x["repo"], x.get("merged_at", ""))):
        key = (r["repo"], r["merge_hash"][:10])
        if key in existing:
            continue
        cf = r.get("conformance_score") or {}
        reason = (cf.get("summary") or "").replace("|", "/").strip()[:120]
        tapd = r.get("tapd_id") or ""
        branch = (r.get("branch") or "").replace("|", "/")
        merged = (r.get("merged_at") or "")[:10]
        new_lines.append(
            f"| {r['repo']} | {r['merge_hash'][:10]} | {branch} | {merged} | {tapd} | {reason} |  |"
        )

    if not new_lines:
        return
    lines = head + new_lines
    if tail:
        lines += [""] + tail
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_report(
    res_dir: Path,
    ordered: list[dict],
    tapd: dict[str, dict],
    idx: dict,
    token_usage: dict[str, int],
    *,
    delivery_filter: callable | None = None,
    report_suffix: str = "",
) -> str:
    date = _dt.date.today().strftime("%Y%m%d")
    all_rows = list(_load_rows(res_dir / "merge_scores.jsonl").values())
    if report_suffix == "_mine":
        # 个人报告：结果行里已有 ownership 字段，避免用 delivery_filter（row 缺少 commits）
        rows = [r for r in all_rows if r.get("ownership") in ("lead", "participant")]
    else:
        rows = [r for r in all_rows if delivery_filter is None or delivery_filter(r)]

    suspected = [r for r in rows if _is_suspected_wrong_link(r)]
    suspected_keys = {(r["repo"], r["merge_hash"]) for r in suspected}
    _append_suspected_to_pending(suspected, DATASET_DIR / "links_pending_review.md")

    n_linked = sum(1 for r in rows if r.get("tapd_id"))
    n_unlinked = sum(1 for r in rows if not r.get("tapd_id") and not r.get("error"))
    n_err = sum(1 for r in rows if r.get("error"))
    n_trunc = sum(1 for r in rows if r.get("truncated"))

    # token / cost
    prompt_tok = token_usage.get("prompt", 0)
    completion_tok = token_usage.get("completion", 0)
    total_tok = token_usage.get("total", 0)
    calls = token_usage.get("calls", 0)
    # DeepSeek v4-flash 官方价（未命中缓存）：输入 ¥1/M，输出 ¥2/M
    cny_cost = (prompt_tok / 1_000_000) * 1.0 + (completion_tok / 1_000_000) * 2.0
    usd_cost = cny_cost / 7.2

    title = "个人扫描报告" if report_suffix == "_mine" else "全量扫描报告"
    # verify 摘要（apply-verify 落盘）——展示 verify 前后对比
    verify_summary: dict | None = None
    vs_path = DATASET_DIR / "verify_apply_summary.json"
    if vs_path.exists():
        try:
            verify_summary = json.loads(vs_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            verify_summary = None
    lines = [
        f"# {title} {date}",
        "",
        f"- 扫描行数: {len(rows)}（本次目标 deliveries 共 {len(ordered)}）",
        f"- 有关联 story: {n_linked} / 无关联(仅摘要): {n_unlinked} / 失败: {n_err}",
        f"- truncated: {n_trunc}（占比 {n_trunc / max(len(rows), 1):.1%}）",
        f"- LLM 调用: {calls} 次 / token: {total_tok}（prompt {prompt_tok}, completion {completion_tok}）",
        f"- 估算成本: ¥{cny_cost:.2f} ≈ ${usd_cost:.2f}（按 deepseek-v4-flash 未命中缓存价）",
        f"- 疑似错链（alignment=1 且判定完全无关）: {len(suspected)} 条，已追加到 dataset/links_pending_review.md",
    ]
    if verify_summary:
        lines.append(
            f"- verify 后分级: accept {verify_summary.get('accepted', 0)} / "
            f"reject {verify_summary.get('rejected', 0)} / 留队列 {verify_summary.get('kept_uncertain', 0)}"
            f"（其中日期守卫 reject {verify_summary.get('guard_rejected', 0)}）"
        )
        lines.append(
            f"- 关联数变化: {verify_summary.get('linked_before', '?')} → {verify_summary.get('linked_after', '?')}"
            f"（verify 前 → verify 后）"
        )

    lines += ["", "## 总体分布", "",
               "| 维度 | 均分 | 中位 | 最小 | 最大 |",
               "|------|------|------|------|------|"]
    by_dim: dict[str, list[int]] = {}
    for r in rows:
        is_suspected = (r["repo"], r["merge_hash"]) in suspected_keys
        for dim, score in _dims(r):
            # 疑似错链的 conformance 分数不计入均分
            if is_suspected and dim.startswith("conf."):
                continue
            by_dim.setdefault(dim, []).append(score)
    for dim, vals in sorted(by_dim.items()):
        lines.append(f"| {dim} | {mean(vals):.2f} | {sorted(vals)[len(vals)//2]} | {min(vals)} | {max(vals)} |")

    hist: dict[int, int] = {}
    for vals in by_dim.values():
        for v in vals:
            hist[v] = hist.get(v, 0) + 1
    lines += ["", "| 分数 | 出现次数 |", "|------|----------|"]
    for s in sorted(hist):
        lines.append(f"| {s} | {hist[s]} |")

    lines += ["", "## 疑似错链（alignment=1 且判定为完全无关）", ""]
    if suspected:
        lines.append(
            f"共 {len(suspected)} 条，已追加到 `dataset/links_pending_review.md` 等待确认；"
            "这些 case **不计入 conformance 均分** 与 drift 列表。"
        )
        for r in suspected[:30]:
            cf = r["conformance_score"]
            merged = r.get("merged_at", "")[:10]
            lines.append(
                f"- **{r['repo']}:{r['merge_hash'][:10]}** ({merged}) "
                f"tapd={r.get('tapd_id', '')} alignment={cf['alignment']} coverage={cf['coverage']} scope_drift={cf['scope_drift']}"
            )
            if cf.get("summary"):
                lines.append(f"  - summary: {cf['summary'][:120]}")
    else:
        lines.append("（无）")

    lines += ["", "## 真 drift case（按严重度排序，不含疑似错链）", ""]
    drift_rows = [
        r for r in rows
        if (r.get("conformance_score") or {}).get("alignment") is not None
        and not r.get("error")
        and (r["repo"], r["merge_hash"]) not in suspected_keys
    ]
    drift = sorted(drift_rows, key=_drift_severity)
    if drift:
        for r in drift[:30]:
            cf = r["conformance_score"]
            merged = r.get("merged_at", "")[:10]
            lines.append(
                f"- **{r['repo']}:{r['merge_hash'][:10]}** ({merged}) "
                f"alignment={cf['alignment']} coverage={cf['coverage']} scope_drift={cf['scope_drift']}"
            )
            if cf.get("summary"):
                lines.append(f"  - summary: {cf['summary'][:120]}")
            if cf.get("findings"):
                for f in cf["findings"][:3]:
                    lines.append(f"  - {f[:160]}")
    else:
        lines.append("（无 conformance 评分）")

    def _conf_dim(confs: list[dict], dim: str) -> list[int]:
        return [c[dim] for c in confs if isinstance(c.get(dim), int)]

    def _conf_rows(rs: list[dict]) -> list[dict]:
        return [
            r for r in rs
            if (r.get("conformance_score") or {}).get("alignment") is not None
            and (r["repo"], r["merge_hash"]) not in suspected_keys
        ]

    if verify_summary:
        lines += ["", "## verify 前后对比", ""]
        lines += ["| 指标 | verify 前 | verify 后 |", "|------|-----------|-----------|"]
        lines.append(
            f"| 关联数（mine，stories_matched high+） | {verify_summary.get('linked_before', '?')} | "
            f"{verify_summary.get('linked_after', '?')} |"
        )
        conf_vals = _conf_dim([r["conformance_score"] for r in _conf_rows(rows)], "alignment")
        if conf_vals:
            lines.append(
                f"| conf.alignment 均分（不含疑似错链） | - | {mean(conf_vals):.2f}（{len(conf_vals)} 个） |"
            )
        else:
            lines.append("| conf.alignment 均分 | - | - |")
        lines.append("")

    lines += ["", "## 按 repo", ""]
    by_repo: dict[str, list[dict]] = {}
    for r in rows:
        by_repo.setdefault(r["repo"], []).append(r)
    for repo, rs in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
        confs = [r["conformance_score"] for r in _conf_rows(rs)]
        vals = _conf_dim(confs, "alignment")
        if vals:
            lines.append(f"- {repo}: {len(rs)} 个,conf.alignment 均分 {mean(vals):.2f}")
        else:
            lines.append(f"- {repo}: {len(rs)} 个（无 conformance）")

    lines += ["", "## 按月份", ""]
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(_month_key(r.get("merged_at", "")), []).append(r)
    for month, rs in sorted(by_month.items()):
        confs = [r["conformance_score"] for r in _conf_rows(rs)]
        vals = _conf_dim(confs, "alignment")
        avg = mean(vals) if vals else float("nan")
        in_pipe = sum(1 for r in rs if r.get("story_key"))
        lines.append(
            f"- {month}: {len(rs)} 个,conf.alignment 均分 {avg:.2f},管线内 {in_pipe} 个"
        )

    lines += ["", "## 管线内 vs 管线外 ★", ""]
    in_pipe = [r for r in rows if r.get("story_key")]
    out_pipe = [r for r in rows if not r.get("story_key")]
    def _avg_scores(rs: list[dict], dim: str) -> str:
        vals = _conf_dim([r["conformance_score"] for r in _conf_rows(rs)], dim)
        return f"{mean(vals):.2f}" if vals else "-"
    lines.append(f"- 管线内（有 story_key）: {len(in_pipe)} 个")
    lines.append(f"  - alignment {_avg_scores(in_pipe, 'alignment')} / coverage {_avg_scores(in_pipe, 'coverage')} / scope_drift {_avg_scores(in_pipe, 'scope_drift')}")
    lines.append(f"- 管线外: {len(out_pipe)} 个")
    lines.append(f"  - alignment {_avg_scores(out_pipe, 'alignment')} / coverage {_avg_scores(out_pipe, 'coverage')} / scope_drift {_avg_scores(out_pipe, 'scope_drift')}")
    if in_pipe and out_pipe:
        out_a = _avg_scores(out_pipe, 'alignment')
        in_a = _avg_scores(in_pipe, 'alignment')
        if out_a != "-" and in_a != "-":
            gap = float(out_a) - float(in_a)
            lines.append(f"- 对齐差距: alignment {gap:+.2f}（管线外 - 管线内）")

    lines += ["", "## 按 author", ""]
    by_author: dict[str, list[dict]] = {}
    for r in rows:
        by_author.setdefault(normalize_author(r.get("author") or "?"), []).append(r)
    for author, rs in sorted(by_author.items(), key=lambda kv: -len(kv[1]))[:15]:
        confs = [r["conformance_score"] for r in _conf_rows(rs)]
        vals = _conf_dim(confs, "alignment")
        avg = mean(vals) if vals else float("nan")
        lines.append(f"- {author}: {len(rs)} 个,conf.alignment 均分 {avg:.2f}")

    errs = [r for r in rows if r.get("error")]
    if errs:
        lines += ["", "## 失败列表", ""]
        for r in errs[:50]:
            lines.append(f"- {r['repo']}:{r['merge_hash'][:10]}: {r['error'][:120]}")
    return "\n".join(lines) + "\n"
