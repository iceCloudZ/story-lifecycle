r"""三方匹配 — A(merge 交付单元) × B(TAPD story) × C(story.db + 证据目录)。

信号优先级（低置信度一律进待确认队列,不强行关联）:

**A↔B**:
1. TAPD ``get-commit-msg`` 官方关联（tapd_commits.jsonl）→ official
2. 分支名含 id（``tapd-(?:bug_)?(\d{18,19})`` / ``(?:feature/(?:\w+/)?|_)(\d{7})``,
   短 id 补前缀 ``114438189600``）→ high
3. LLM 模糊匹配（``--llm`` 才跑,需 OPENCODE_API_KEY）→ ≥0.8 medium / 0.5-0.8 待确认 / <0.5 不关联

**A↔C**: 4. commit hash 精确种子（branch_bound ``hc-user:1528cc41`` 式 + change_item）
→ high;5. 分支名精确种子（branch_bound evidence.branches）→ high

**B↔C**: story.source_id / tapd_url 长 id → high;story_key 数字段 = TAPD id → high

输出: ``stories_matched.jsonl``（tapd_id 主键统一实体）+ ``links_pending_review.md``
（待人工确认）+ ``coverage_report.md``（覆盖率 + 孤儿清单）。
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import dataset as ds
from . import gitindex

log = logging.getLogger("eval.linker")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = PACKAGE_ROOT / "dataset"

LONG_PREFIX = "114438189600"
LONG_ID_RE = re.compile(r"tapd-(?:bug_)?(\d{18,19})")
# 短 id 实测模式:feat_1034334_tjy / feature-1067018-x / feature/cjc/feature-1028182-x /
# feat/tjy/1065006 / feature/zzh/1065570_x ... 统一规则:任意分隔的 7 位数字且以 10 开头
# （TAPD story id 特征;日期串 20240522 等以 20 开头自然排除）
SHORT_ID_RE = re.compile(r"(?<!\d)(10\d{5})(?!\d)")
LONG_GENERIC_RE = re.compile(r"(?<!\d)(114438189600\d{7})(?!\d)")
BRANCH_SEED_RE = re.compile(r"(feature|release|hotfix|master|main)/[A-Za-z0-9_./\-]+")
HASH_SEED_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
REPO_HASH_RE = re.compile(r"([A-Za-z0-9_.\-]+):([0-9a-f]{7,40})")
REPO_COMMIT_RE = re.compile(r"([A-Za-z0-9_.\-]+) commit ([0-9a-f]{7,40})")
DB_REF = r"C:/Users/zzh58/.story-lifecycle/story.db"


def long_id(short: str) -> str:
    return f"{LONG_PREFIX}{short}"


def short_id(long: str) -> str:
    return long[len(LONG_PREFIX):] if long.startswith(LONG_PREFIX) else long


def extract_ids_from_branch(branch: str) -> list[tuple[str, str]]:
    """从分支名提取 (long_tapd_id, method)。"""
    out: list[tuple[str, str]] = []
    if not branch:
        return out
    for m in LONG_ID_RE.finditer(branch):
        out.append((m.group(1), "branch_tapd_prefix"))
    for m in LONG_GENERIC_RE.finditer(branch):
        tid = m.group(1)
        if not any(tid == t for t, _ in out):
            out.append((tid, "branch_long_id"))
    for m in SHORT_ID_RE.finditer(branch):
        short = m.group(1)
        if not any(short == short_id(t) for t, _ in out):
            out.append((long_id(short), "branch_short_id"))
    return out


# ---------------------------------------------------------------------------
# C 源读取
# ---------------------------------------------------------------------------


def load_c_sources(db_path: str = DB_REF) -> dict:
    """读 story.db（只读）:stories + branch_bound 种子 + change_item 种子。"""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    stories: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT id, story_key, title, workspace, profile, status, source_type, source_id, "
        "tapd_url, tapd_status, is_test, deleted_at FROM story"
    ):
        d = dict(r)
        key = d["story_key"]
        d["id"] = int(d["id"])
        stories[key] = d

    id_to_key = {s["id"]: k for k, s in stories.items()}

    # branch_bound 种子:hash + branch → story
    hash_seeds: dict[str, set[str]] = {}
    branch_seeds: dict[str, set[str]] = {}
    for r in conn.execute("SELECT story_id, detail FROM gate_result WHERE gate_name='branch_bound'"):
        story_key = id_to_key.get(r["story_id"])
        if not story_key:
            continue
        detail = r["detail"] or ""
        try:
            obj = json.loads(detail)
            ev = obj.get("evidence") or {}
            for b in ev.get("branches") or []:
                if isinstance(b, dict) and b.get("branch"):
                    branch_seeds.setdefault(b["branch"], set()).add(story_key)
        except (json.JSONDecodeError, TypeError):
            pass
        # evidence_ref 变体
        for repo, h in REPO_HASH_RE.findall(detail):
            hash_seeds.setdefault(h.lower(), set()).add(story_key)
        for repo, h in REPO_COMMIT_RE.findall(detail):
            hash_seeds.setdefault(h.lower(), set()).add(story_key)
        for h in HASH_SEED_RE.findall(detail):
            if len(h) >= 7:
                hash_seeds.setdefault(h.lower(), set()).add(story_key)
        for m in BRANCH_SEED_RE.finditer(detail):
            branch_seeds.setdefault(m.group(0), set()).add(story_key)

    # change_item 种子
    for r in conn.execute("SELECT story_key, evidence_ref FROM story_change_item"):
        for h in HASH_SEED_RE.findall(r["evidence_ref"] or ""):
            if len(h) >= 7:
                hash_seeds.setdefault(h.lower(), set()).add(r["story_key"])

    # B↔C:B 侧 id 映射（story → tapd long id）
    story_tapd: dict[str, str] = {}
    for key, s in stories.items():
        tid = ""
        src = (s.get("source_id") or "").replace("bug_", "")
        if re.fullmatch(r"\d{15,20}", src):
            tid = src
        else:
            # 老行 source_id 为 NULL → 从 story_key 推导:
            # tapd-1144381896001066924 / tapd-bug_114... / 纯数字短 id 1064584
            m = re.fullmatch(r"tapd-(?:bug_)?(\d{15,20})", key)
            if m:
                tid = m.group(1)
            else:
                m = re.fullmatch(r"(\d{7})", key)
                if m:
                    tid = long_id(m.group(1))
        if not tid:
            m = re.search(r"(\d{18,19})", s.get("tapd_url") or "")
            if m:
                tid = m.group(1)
        if tid:
            story_tapd[key] = tid

    conn.close()
    return {
        "stories": stories,
        "hash_seeds": hash_seeds,
        "branch_seeds": branch_seeds,
        "story_tapd": story_tapd,
    }


# ---------------------------------------------------------------------------
# B 源读取
# ---------------------------------------------------------------------------


def load_b_sources() -> dict:
    tapd_stories: dict[str, dict] = {}
    if ds.DATASET_DIR.joinpath("tapd_stories.jsonl").exists():
        for line in ds.DATASET_DIR.joinpath("tapd_stories.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                tapd_stories[rec["tapd_id"]] = rec
    tapd_commits: dict[str, list[str]] = {}
    p = ds.DATASET_DIR.joinpath("tapd_commits.jsonl")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            hashes = tapd_commits.setdefault(rec["tapd_id"], [])
            for h in HASH_SEED_RE.findall(str(rec.get("commit") or rec.get("hash") or rec.get("raw") or "")):
                hashes.append(h.lower())
            if "commit" in rec and isinstance(rec["commit"], dict) and rec["commit"].get("sha"):
                hashes.append(str(rec["commit"]["sha"]).lower())
    return {"stories": tapd_stories, "commits": tapd_commits}


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------


def _delivery_id(d: dict) -> str:
    return f"{d['repo']}:{d['merge_hash']}"


def match_a_c(deliveries: list[dict], c: dict) -> dict[str, list[dict]]:
    """A↔C:hash/分支种子 → {delivery_id: [story links]}。"""
    out: dict[str, list[dict]] = {}
    hash_seeds = c["hash_seeds"]
    branch_seeds = c["branch_seeds"]
    for d in deliveries:
        links: list[dict] = []
        seen: set[str] = set()
        # hash 种子
        for commit in d.get("commits", []):
            h = (commit.get("hash") or "").lower()
            for seed, keys in hash_seeds.items():
                if h.startswith(seed) and seed in h[:8]:
                    for k in keys:
                        if k not in seen:
                            links.append({"story_key": k, "method": "seed_hash", "confidence": "high"})
                            seen.add(k)
        # 分支种子
        for seed, keys in branch_seeds.items():
            if d.get("branch") == seed or (seed and d.get("branch", "").startswith(seed)):
                for k in keys:
                    if k not in seen:
                        links.append({"story_key": k, "method": "seed_branch", "confidence": "high"})
                        seen.add(k)
        if links:
            out[_delivery_id(d)] = links
    return out


def match_a_b(deliveries: list[dict], b: dict) -> dict[str, list[dict]]:
    """A↔B:官方 commit-msg 种子 + 分支名 id → {delivery_id: [tapd links]}。"""
    out: dict[str, list[dict]] = {}
    commit_index: dict[str, list[str]] = {}  # hash(前8) → [tapd_id]
    for tid, hashes in b.get("commits", {}).items():
        for h in hashes:
            commit_index.setdefault(h[:8], set()).add(tid)  # type: ignore[arg-type]
    commit_index = {k: list(v) for k, v in commit_index.items()}

    for d in deliveries:
        links: list[dict] = []
        seen: set[str] = set()
        # 官方关联
        for commit in d.get("commits", []):
            h = (commit.get("hash") or "").lower()
            for prefix, tids in commit_index.items():
                if h.startswith(prefix):
                    for t in tids:
                        if t not in seen:
                            links.append({"tapd_id": t, "method": "official_commit", "confidence": "official"})
                            seen.add(t)
        # 分支名 id
        for tid, method in extract_ids_from_branch(d.get("branch", "")):
            if tid not in seen:
                links.append({"tapd_id": tid, "method": method, "confidence": "high"})
                seen.add(tid)
        if links:
            out[_delivery_id(d)] = links
    return out


def _conf_rank(conf: str) -> int:
    return {"": 0, "pending": 1, "medium": 2, "high": 3, "official": 4}.get(conf or "", 0)


def _add_delivery(e: dict, d: dict, method: str, confidence: str) -> None:
    """向实体追加交付单元;按 (repo, merge_hash) 去重,保留最高置信度。"""
    for ex in e["deliveries"]:
        if ex["repo"] == d["repo"] and ex["merge_hash"] == d["merge_hash"]:
            if _conf_rank(ex.get("confidence", "")) < _conf_rank(confidence):
                ex["link_method"] = method
                ex["confidence"] = confidence
            return
    e["deliveries"].append(
        {
            "repo": d["repo"],
            "merge_hash": d["merge_hash"],
            "branch": d.get("branch", ""),
            "link_method": method,
            "confidence": confidence,
        }
    )


def build_entities(
    deliveries: list[dict],
    a_c: dict[str, list[dict]],
    a_b: dict[str, list[dict]],
    c: dict,
    b: dict,
    confirmed_dids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """三方汇合 → (entities, pending_review)。以 tapd_id 为主键。

    ``confirmed_dids`` 为已人工确认的 delivery（(repo,merge_hash) 集合）——
    这些 merge 的候选冲突视为已解决,不再进待确认队列。merge_hash 支持
    10 位短前缀（link_confirmations.jsonl 存的是前缀）。
    """
    c_stories = c["stories"]
    story_tapd = c["story_tapd"]
    tapd_stories = b.get("stories", {})

    # C→tapd 反向映射（B↔C high）
    tapd_to_story: dict[str, str] = {}
    for key, tid in story_tapd.items():
        tapd_to_story.setdefault(tid, key)

    entities: dict[str, dict] = {}
    pending: list[dict] = []

    def entity_for_tapd(tid: str) -> dict:
        if tid not in entities:
            e = {
                "tapd_id": tid,
                "name": (tapd_stories.get(tid) or {}).get("name", ""),
                "status": (tapd_stories.get(tid) or {}).get("status", ""),
                "iteration_id": (tapd_stories.get(tid) or {}).get("iteration_id", ""),
                "owner": (tapd_stories.get(tid) or {}).get("owner", ""),
                "story_key": "",
                "story_title": "",
                "evidence_dir": "",
                "deliveries": [],
                "link_summary": {"A_B": "", "A_C": "", "B_C": ""},
                "link_notes": [],
            }
            sk = tapd_to_story.get(tid)
            if sk:
                e["story_key"] = sk
                e["story_title"] = c_stories.get(sk, {}).get("title", "")
                e["B_C"] = "high"  # noqa
                e["link_summary"]["B_C"] = "high"
                e["evidence_dir"] = _find_evidence_dir(c_stories[sk])
            entities[tid] = e
        return entities[tid]

    def entity_for_story(sk: str) -> dict:
        """C-only story（无 tapd 关联）→ 以 story_key 为实体键。"""
        ekey = f"story:{sk}"
        if ekey not in entities:
            e = {
                "tapd_id": "",
                "name": c_stories.get(sk, {}).get("title", ""),
                "status": "",
                "iteration_id": "",
                "owner": "",
                "story_key": sk,
                "story_title": c_stories.get(sk, {}).get("title", ""),
                "evidence_dir": _find_evidence_dir(c_stories[sk]),
                "deliveries": [],
                "link_summary": {"A_B": "", "A_C": "", "B_C": ""},
                "link_notes": ["C-only:story.db 无 TAPD 关联"],
            }
            entities[ekey] = e
        return entities[ekey]

    # B↔C 先行:让 tapd 实体带上 story 信息
    for key, tid in story_tapd.items():
        entity_for_tapd(tid)

    # A↔B / A↔C 挂载
    for d in deliveries:
        did = _delivery_id(d)
        tapd_links = a_b.get(did, [])
        story_links = a_c.get(did, [])

        # A↔C → 经 story→tapd 找到实体
        for sl in story_links:
            sk = sl["story_key"]
            tid = story_tapd.get(sk)
            e = entity_for_tapd(tid) if tid else entity_for_story(sk)
            _add_delivery(e, d, sl["method"], sl["confidence"])
            e["link_summary"]["A_C"] = _max_conf(e["link_summary"]["A_C"], sl["confidence"])

        # A↔B
        for tl in tapd_links:
            e = entity_for_tapd(tl["tapd_id"])
            _add_delivery(e, d, tl["method"], tl["confidence"])
            e["link_summary"]["A_B"] = _max_conf(e["link_summary"]["A_B"], tl["confidence"])

        # 冲突/多候选:同一 delivery 挂到 ≥2 个不同 tapd 实体
        # （已人工确认的 delivery 跳过——其候选冲突已被确认结果解决）
        tapd_targets = {tl["tapd_id"] for tl in tapd_links}
        confirmed = confirmed_dids or set()
        h10 = d["merge_hash"][:10]
        if len(tapd_targets) > 1 and not (
            (did in confirmed) or (f"{d['repo']}:{h10}" in confirmed)
        ):
            pending.append(
                {
                    "kind": "conflict_ab",
                    "repo": d["repo"],
                    "merge_hash": d["merge_hash"],
                    "branch": d.get("branch", ""),
                    "merged_at": d.get("merged_at", ""),
                    "candidates": sorted(tapd_targets),
                    "reason": "同一 merge 匹配多个 TAPD story（低置信冲突）",
                }
            )

    entities_list = list(entities.values())
    # C-only 且无交付的实体也保留（覆盖率用）
    return entities_list, pending


def _find_evidence_dir(story: dict) -> str:
    """复用 dataset 的证据目录映射,返回存在的第一个证据目录。"""
    dirs = ds._find_evidence_dirs(story.get("workspace", ""), story.get("story_key", ""))
    return str(dirs[0]) if dirs else ""


def _max_conf(a: str, b: str) -> str:
    order = ["", "medium", "high", "official"]
    return a if order.index(a) >= order.index(b) else b


# ---------------------------------------------------------------------------
# LLM 模糊匹配（A↔B 信号 3;需 OPENCODE_API_KEY）
# ---------------------------------------------------------------------------


def llm_match_unmatched(
    deliveries: list[dict],
    a_b: dict[str, list[dict]],
    b: dict,
    tapd_stories: dict,
    limit: int = 500,
) -> dict[str, list[dict]]:
    """对无种子关联的 delivery 做 LLM 模糊匹配（串行,temperature=0）。"""
    from .judges import _LLM
    from pydantic import BaseModel, Field

    class MatchOut(BaseModel):
        tapd_id: str = Field(description="候选 TAPD story 的完整 id（原样返回,无匹配则空串）")
        confidence: float = Field(ge=0, le=1, description="匹配置信度 0-1")

    matched: dict[str, list[dict]] = {}
    candidates = [
        d for d in deliveries
        if not a_b.get(_delivery_id(d)) and d.get("branch")
    ]
    log.info("LLM 待匹配 delivery: %d（cap %d）", len(candidates), limit)
    n = 0
    for d in candidates[:limit]:
        n += 1
        # ±45 天窗口候选
        from datetime import datetime, timedelta

        merged_at = d.get("merged_at", "")
        window = (datetime.fromisoformat(merged_at), 45) if merged_at else (None, None)
        pool = []
        for tid, rec in tapd_stories.items():
            t = rec.get("created") or rec.get("modified") or ""
            if window[0] is None:
                pool.append((tid, rec["name"]))
                continue
            try:
                ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
                if abs((ts - window[0]).days) <= window[1]:
                    pool.append((tid, rec["name"]))
            except ValueError:
                pool.append((tid, rec["name"]))
        pool = pool[:60]
        if not pool:
            continue
        subjects = "; ".join(f"{c.get('subject','')}" for c in d.get("commits", [])[:15])
        prompt = (
            "判断一个代码交付 merge 对应哪个 TAPD 需求 story。\n"
            f"merge: repo={d['repo']} branch={d['branch']} merged_at={merged_at}\n"
            f"commit 摘要: {subjects[:2000]}\n\n"
            f"候选 TAPD story:\n" + "\n".join(f"- {tid}: {name[:120]}" for tid, name in pool)
            + "\n\n只输出 JSON: {\"tapd_id\": \"选中的完整 id(无匹配则空串)\", \"confidence\": 0-1}。"
            "confidence ≥0.8 才算可靠;拿不准给 0.5-0.7。"
        )
        try:
            res = _LLM.invoke_structured(prompt, MatchOut)
            tid, conf = res.tapd_id, res.confidence
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 匹配失败 %s: %s", _delivery_id(d), e)
            continue
        if not tid or tid not in {t for t, _ in pool}:
            continue
        if conf >= 0.5:
            matched[_delivery_id(d)] = [
                {
                    "tapd_id": tid,
                    "method": "llm_fuzzy",
                    "confidence": "medium" if conf >= 0.8 else "pending",
                    "score": round(conf, 2),
                }
            ]
        if n % 20 == 0:
            log.info("LLM 匹配进度 %d/%d", n, min(limit, len(candidates)))
    return matched


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_link(do_llm: bool = False, llm_limit: int = 500) -> dict:
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    deliveries = gitindex.load_deliveries()
    if not deliveries:
        raise RuntimeError("deliveries.jsonl 为空——先跑 `eval index`")
    c = load_c_sources()
    b = load_b_sources()

    a_c = match_a_c(deliveries, c)
    a_b = match_a_b(deliveries, b)
    log.info("A-C 种子匹配: %d 个 delivery;A-B 种子匹配: %d 个 delivery", len(a_c), len(a_b))

    # 人工确认结果自动采纳（official）;link_confirmations 存的是 10 位 hash 前缀,
    # 需解析回 deliveries.jsonl 的全 hash 才能挂到 a_b 的正式键上
    conf_path = DATASET_DIR / "link_confirmations.jsonl"
    confirmed_dids: set[str] = set()
    confirmed_by_did: dict[str, list[str]] = {}
    full_by_prefix: dict[tuple[str, str], str] = {
        (d["repo"], d["merge_hash"][:10]): d["merge_hash"] for d in deliveries
    }
    if conf_path.exists():
        for line in conf_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            full = full_by_prefix.get((rec["repo"], rec["merge_hash"][:10]), rec["merge_hash"])
            did = f"{rec['repo']}:{full}"
            confirmed_dids.add(did)
            confirmed_by_did.setdefault(did, []).append(rec["tapd_id"])
            a_b.setdefault(did, []).append(
                {"tapd_id": rec["tapd_id"], "method": "user_confirm", "confidence": "official"}
            )
        # 冲突消解:已确认的 merge,未被确认的分支名候选链接移除
        # （人工确认即最终裁决;official_commit 官方关联不受影响）
        branch_methods = ("branch_short_id", "branch_long_id", "branch_tapd_prefix")
        for did, tids in confirmed_by_did.items():
            links = a_b.get(did, [])
            a_b[did] = [
                l for l in links
                if l["tapd_id"] in tids or l["method"] not in branch_methods
            ]

    if do_llm:
        llm_links = llm_match_unmatched(deliveries, a_b, b, b.get("stories", {}), limit=llm_limit)
        a_b = {**a_b, **llm_links}
        log.info("LLM 模糊匹配新增: %d", len(llm_links))

    entities, pending = build_entities(deliveries, a_c, a_b, c, b, confirmed_dids=confirmed_dids)

    # 落盘
    _write_jsonl(DATASET_DIR / "stories_matched.jsonl", entities)
    _write_pending_md(pending, DATASET_DIR / "links_pending_review.md")
    coverage = _coverage_report(deliveries, entities, c, b, a_c, a_b)
    (DATASET_DIR / "coverage_report.md").write_text(coverage["md"], encoding="utf-8")

    # 统计
    ab_high = sum(
        1 for e in entities if e["link_summary"]["A_B"] in ("high", "official")
    )
    both_delivery = sum(1 for e in entities if e["deliveries"] and e["evidence_dir"])
    log.info(
        "实体 %d;A∩B high/official %d;有交付+证据目录 %d;待确认 %d",
        len(entities), ab_high, both_delivery, len(pending),
    )
    return {
        "entities": len(entities),
        "ab_high": ab_high,
        "with_delivery_and_evidence": both_delivery,
        "pending": len(pending),
        "coverage": coverage["summary"],
    }


def _write_jsonl(path: Path, items: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _write_pending_md(pending: list[dict], path: Path) -> None:
    lines = [
        "# 待确认链接队列（人工标注后跑 `eval review-apply`）",
        "",
        "标注方法:在每行 `|` 分隔的表格里,把「决策」列填为 `accept:1144381896001065570`",
        "（接受某候选）/ `reject`（不关联）;保存后跑 review-apply。",
        "",
        "| repo | merge | branch | merged_at | 候选 TAPD | 理由 | 决策 |",
        "|------|-------|--------|-----------|-----------|------|------|",
    ]
    existing: set[tuple[str, str]] = set()
    # 保留旧文件表格之后的附录小节（判断依据 / 人工备注等）,避免重跑时丢失
    tail: list[str] = []
    if path.exists():
        old = path.read_text(encoding="utf-8").splitlines()
        in_tail = False
        for ln in old:
            if ln.startswith("## ") or ln.startswith("### "):
                in_tail = True
            if in_tail:
                tail.append(ln)
                continue
            if ln.startswith("| "):
                cells = [c.strip() for c in ln.split("|")]
                if len(cells) >= 4 and cells[1] != "repo" and set(cells[1]) != {"-"}:
                    existing.add((cells[1], cells[2]))
            if not in_tail:
                lines.append(ln)
    for p in pending:
        key = (p["repo"], p["merge_hash"][:10])
        if key in existing:
            continue
        existing.add(key)
        branch = (p.get("branch") or "").replace("|", "/")
        reason = (p["reason"] or "").replace("|", "/")
        lines.append(
            f"| {p['repo']} | {p['merge_hash'][:10]} | {branch} | {p.get('merged_at','')[:10]} | "
            f"{'; '.join(p['candidates'])} | {reason} |  |"
        )
    if tail:
        lines += ["", *tail]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _coverage_report(
    deliveries: list[dict],
    entities: list[dict],
    c: dict,
    b: dict,
    a_c: dict,
    a_b: dict,
) -> dict:
    n_a = len(deliveries)
    n_b = len(b.get("stories", {}))
    n_c = len(c["stories"])
    with_tapd = {e["tapd_id"] for e in entities if e["tapd_id"]}
    with_story = {e["story_key"] for e in entities if e["story_key"]}
    a_b_set = {e["tapd_id"] for e in entities if e["link_summary"]["A_B"]}
    a_c_set = {e["story_key"] for e in entities if e["link_summary"]["A_C"]}
    b_c_set = {e["story_key"] for e in entities if e["story_key"] and e["link_summary"]["B_C"]}
    abc = a_b_set & {v for k, v in c["story_tapd"].items() if v in with_tapd}

    lines = [
        "# 三方覆盖率报告",
        "",
        f"- A 交付单元(merge): {n_a}",
        f"- B TAPD story: {n_b}",
        f"- C story.db 记录: {n_c}",
        "",
        "## 交集",
        "",
        f"- A∩B（有交付且有 TAPD 关联）: {len(a_b_set)}（其中 high/official: "
        f"{sum(1 for e in entities if e['link_summary']['A_B'] in ('high','official'))}）",
        f"- A∩C（有交付且有 story.db 记录）: {len(a_c_set)}",
        f"- B∩C（TAPD ↔ story.db）: {len(b_c_set)}",
        f"- A∩B∩C: {len(abc)}",
        "",
        "## 孤儿清单",
        "",
    ]
    # 有 TAPD 无交付
    no_delivery = [e["tapd_id"] for e in entities if e["tapd_id"] and not e["deliveries"]]
    lines.append(f"### 有 TAPD 无交付（{len(no_delivery)}）")
    for tid in no_delivery[:50]:
        lines.append(f"- {tid}: {(b.get('stories', {}).get(tid) or {}).get('name', '')[:60]}")
    # 有交付无 TAPD story（C-only 或未匹配）
    orphan_deliveries = [
        d for d in deliveries
        if not a_b.get(f"{d['repo']}:{d['merge_hash']}")
        and not a_c.get(f"{d['repo']}:{d['merge_hash']}")
    ]
    lines.append(f"### 有交付无任何关联（{len(orphan_deliveries)}）")
    for d in orphan_deliveries[:50]:
        lines.append(f"- {d['repo']}:{d['merge_hash'][:10]} {d.get('branch','')[:70]}")
    # story.db 有记录 TAPD 查无
    tapd_ids = set(b.get("stories", {}).keys())
    no_tapd = [
        k for k, v in c["story_tapd"].items()
        if v not in tapd_ids
    ]
    lines.append(f"### story.db 有 TAPD 记录但 B 源查无（{len(no_tapd)}）")
    for k in no_tapd[:50]:
        lines.append(f"- {k} → {c['story_tapd'][k]}")
    return {"summary": {"A": n_a, "B": n_b, "C": n_c, "AB": len(a_b_set), "AC": len(a_c_set), "BC": len(b_c_set), "ABC": len(abc)}, "md": "\n".join(lines) + "\n"}


def review_apply(path: str | Path) -> dict:
    """解析人工标注后的待确认表格,accept 项落 link_confirmations.jsonl。

    link 下次运行时自动采纳为 official 关联（method=user_confirm）。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    confirmed: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or "| accept" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        repo, merge, branch, merged_at, candidates, reason, decision = cells
        if decision.startswith("accept:"):
            tid = decision.split(":", 1)[1].strip()
            confirmed.append(
                {"repo": repo, "merge_hash": merge, "branch": branch, "tapd_id": tid, "decision": "accept"}
            )
    if not confirmed:
        return {"applied": 0, "message": "没有找到 accept 标注"}
    path_out = DATASET_DIR / "link_confirmations.jsonl"
    existing = []
    if path_out.exists():
        existing = [json.loads(x) for x in path_out.read_text(encoding="utf-8").splitlines() if x.strip()]
    merged = existing + [c for c in confirmed if c not in existing]
    path_out.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in merged) + "\n",
        encoding="utf-8",
    )
    return {"applied": len(merged), "file": str(path_out)}


# ---------------------------------------------------------------------------
# 个人 merge 加强版 LLM 关联
# ---------------------------------------------------------------------------


def load_stories_matched() -> list[dict]:
    """读取当前 stories_matched.jsonl。"""
    p = DATASET_DIR / "stories_matched.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _linked_delivery_keys(entities: list[dict]) -> set[tuple[str, str]]:
    return {
        (d["repo"], d["merge_hash"])
        for e in entities
        for d in e.get("deliveries", [])
    }


def _filter_tapd_by_owner(tapd_stories: dict[str, dict], owners: set[str]) -> dict[str, dict]:
    """只保留 owner 字段包含任意指定开发者的 TAPD story。"""
    out: dict[str, dict] = {}
    for tid, rec in tapd_stories.items():
        owner = rec.get("owner") or ""
        if any(o in owner for o in owners):
            out[tid] = rec
    return out


def _parse_dt(s: str) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _keywords(text: str) -> set[str]:
    """提取关键词：字母/数字连续片段 + 中文单字，过滤常见停用词。"""
    if not text:
        return set()
    stop = {
        "feature", "fix", "add", "bug", "merge", "branch", "master", "the", "and", "for",
        "to", "of", "in", "on", "with", "from", "into", "hc", "api", "", " ", "一个",
        "需求", "实现", "修复", "添加", "修改", "更新", "分支", "合并", "提交",
    }
    out: set[str] = set()
    for m in re.finditer(r"[a-zA-Z0-9_]+", text):
        w = m.group(0).lower()
        if w not in stop and len(w) >= 2:
            out.add(w)
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            out.add(ch)
    return out


def _candidate_score(delivery: dict, rec: dict) -> float:
    """简单关键词重叠 + 日期接近度得分。"""
    source_text = " ".join(
        [delivery.get("branch", "")]
        + [c.get("subject", "") for c in delivery.get("commits", [])[:15]]
    )
    src = _keywords(source_text)
    tapd_text = f"{rec.get('name', '')} {(rec.get('description') or '')}"
    tapd = _keywords(tapd_text)
    overlap = len(src & tapd)
    score = overlap * 2.0

    # 短 id 在分支/提交中直接命中
    sid = short_id(rec.get("tapd_id", ""))
    if sid and sid in source_text:
        score += 10.0

    merged_dt = _parse_dt(delivery.get("merged_at", ""))
    tapd_dt = _parse_dt(rec.get("created") or rec.get("modified") or "")
    if merged_dt and tapd_dt:
        score += max(0.0, 30.0 - abs((merged_dt.date() - tapd_dt.date()).days))
    return score


def _mine_llm_match_one(
    delivery: dict,
    tapd_pool: dict[str, dict],
    window_days: int,
    auto_threshold: float,
    pending_threshold: float,
    max_candidates: int = 15,
) -> tuple[str, float] | None:
    """对单个 delivery 在我的 TAPD story 池里做 LLM 匹配。

    先按关键词+日期得分选出 top N 候选，再让 LLM 严格 JSON 输出结果。
    """
    from .judges import _LLM
    from pydantic import BaseModel, Field

    class MatchOut(BaseModel):
        tapd_id: str = Field(description="候选 TAPD story 的完整 id（无匹配则空串）")
        confidence: float = Field(ge=0, le=1, description="匹配置信度 0-1")

    merged_dt = _parse_dt(delivery.get("merged_at", ""))
    merged_at = merged_dt.date() if merged_dt else None
    candidates = []
    for tid, rec in tapd_pool.items():
        ts_dt = _parse_dt(rec.get("created") or rec.get("modified") or "")
        ts = ts_dt.date() if ts_dt else None
        if merged_at is None or ts is None:
            candidates.append((tid, rec))
            continue
        if abs((ts - merged_at).days) <= window_days:
            candidates.append((tid, rec))

    if not candidates:
        return None

    # 关键词预排序，只让 LLM 看最相关的 N 个
    candidates.sort(key=lambda x: _candidate_score(delivery, x[1]), reverse=True)
    candidates = candidates[:max_candidates]

    # 如果关键词完全无交集，直接跳过，避免浪费 token
    top_score = _candidate_score(delivery, candidates[0][1])
    if top_score < 2.0:
        return None

    subjects = "; ".join(c.get("subject", "") for c in delivery.get("commits", [])[:15])
    body = (
        "判断以下代码交付 merge 最可能对应哪个 TAPD 需求 story。\n"
        "规则：只选最相关的一个；如果没有可信匹配，confidence 给 0 并返回空 tapd_id。\n"
        "**禁止任何分析、解释或思考过程，直接输出且仅输出一个 JSON 对象。**\n\n"
        f"merge: repo={delivery['repo']} branch={delivery.get('branch', '')} merged_at={delivery.get('merged_at', '')}\n"
        f"commit 摘要: {subjects[:1500]}\n\n"
        "候选 TAPD story（已按负责人和时间窗过滤，按相关度排序）:\n"
    )
    for tid, rec in candidates:
        name = rec.get("name", "")[:120]
        desc = (rec.get("description") or "").replace("\n", " ").strip()[:180]
        body += f"- {tid}: {name}"
        if desc:
            body += f" | {desc}"
        body += "\n"
    body += (
        "\n只输出 JSON 对象:\n"
        f"{{\"tapd_id\": \"选中的完整 id,无匹配则空串\", \"confidence\": 0-1}}\n"
        f"confidence ≥{auto_threshold} 自动关联; {pending_threshold}-{auto_threshold - 0.01:.2f} 进待确认。\n"
        "第一个字符必须是 {{，最后一个字符必须是 }}。"
    )

    try:
        res = _LLM.invoke_structured(body, MatchOut)
        return res.tapd_id, res.confidence
    except Exception as e:  # noqa: BLE001
        log.warning("LLM mine 匹配失败 %s: %s", _delivery_id(delivery), e)
        return None


def run_link_mine(
    window_days: int = 90,
    auto_threshold: float = 0.8,
    pending_threshold: float = 0.5,
    concurrency: int | None = None,
    mine_owners: set[str] | None = None,
    limit: int | None = None,
) -> dict:
    """对未关联的个人 merge 跑加强版 LLM 关联。

    - 只匹配 mine_owners 拥有的 TAPD story（默认 赵子豪）
    - 时间窗放宽到 merge 前后 window_days（默认 90 天）
    - ≥auto_threshold → 直接 high 关联
    - pending_threshold ≤ conf < auto_threshold → 进待确认队列
    """
    from .scanall import classify_ownership

    if concurrency is None:
        concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", "16"))
    if mine_owners is None:
        mine_owners = {"赵子豪"}

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    deliveries = gitindex.load_deliveries()
    entities = load_stories_matched()
    linked_keys = _linked_delivery_keys(entities)

    # 未关联的个人 merge
    mine_unlinked = [
        d for d in deliveries
        if classify_ownership(d) in ("lead", "participant")
        and (d["repo"], d["merge_hash"]) not in linked_keys
    ]

    # 加载并过滤 TAPD stories
    tapd_stories: dict[str, dict] = {}
    tp = DATASET_DIR / "tapd_stories.jsonl"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                tapd_stories[rec["tapd_id"]] = rec
    owner_pool = _filter_tapd_by_owner(tapd_stories, mine_owners)

    log.info(
        "加强版 LLM 关联: 个人未关联 merge %d, 负责人候选 story %d, 并发 %d",
        len(mine_unlinked), len(owner_pool), concurrency,
    )

    # 构建 entity 索引
    entities_by_tapd: dict[str, dict] = {e["tapd_id"]: e for e in entities if e.get("tapd_id")}
    pending_new: list[dict] = []

    auto_count = 0
    pending_count = 0
    lock = threading.Lock()

    def _worker(d: dict) -> tuple[dict, str | None, float]:
        result = _mine_llm_match_one(d, owner_pool, window_days, auto_threshold, pending_threshold)
        return d, (result[0] if result else None), (result[1] if result else 0.0)

    to_process = mine_unlinked if limit is None else mine_unlinked[:limit]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_worker, d): d for d in to_process}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            d, tid, conf = future.result()
            if not tid or tid not in owner_pool:
                continue
            if conf >= auto_threshold:
                with lock:
                    ent = entities_by_tapd.get(tid)
                    if ent is None:
                        rec = owner_pool[tid]
                        ent = {
                            "tapd_id": tid,
                            "name": rec.get("name", ""),
                            "status": rec.get("status", ""),
                            "iteration_id": rec.get("iteration_id", ""),
                            "owner": rec.get("owner", ""),
                            "story_key": "",
                            "story_title": "",
                            "evidence_dir": "",
                            "deliveries": [],
                            "link_summary": {"A_B": "", "A_C": "", "B_C": ""},
                            "link_notes": ["created_by_llm_mine"],
                        }
                        entities_by_tapd[tid] = ent
                        entities.append(ent)
                    _add_delivery(ent, d, "llm_mine_high", "high")
                    ent["link_summary"]["A_B"] = _max_conf(ent["link_summary"]["A_B"], "high")
                    auto_count += 1
            elif conf >= pending_threshold:
                with lock:
                    pending_new.append({
                        "repo": d["repo"],
                        "merge_hash": d["merge_hash"],
                        "branch": d.get("branch", ""),
                        "merged_at": d.get("merged_at", ""),
                        "candidates": [tid],
                        "reason": f"LLM mine 关联置信度 {conf:.2f}（{pending_threshold}-{auto_threshold} 待确认）",
                    })
                    pending_count += 1
            total_proc = len(to_process)
            if i % 50 == 0 or i == total_proc:
                log.info("LLM mine 进度 %d/%d, 自动 %d, 待确认 %d", i, total_proc, auto_count, pending_count)

    # 写回 stories_matched.jsonl
    _write_jsonl(DATASET_DIR / "stories_matched.jsonl", entities)

    # 追加到待确认队列
    if pending_new:
        _write_pending_md(pending_new, DATASET_DIR / "links_pending_review.md")

    return {
        "mine_unlinked": len(mine_unlinked),
        "owner_pool": len(owner_pool),
        "processed": len(to_process),
        "auto_linked": auto_count,
        "pending": pending_count,
        "stories_matched": str(DATASET_DIR / "stories_matched.jsonl"),
        "pending_review": str(DATASET_DIR / "links_pending_review.md"),
    }


if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO, stream=_sys.stdout, encoding="utf-8")
    print(json.dumps(run_link(), ensure_ascii=False))
