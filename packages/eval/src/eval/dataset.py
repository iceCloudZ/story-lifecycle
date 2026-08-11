"""Gold 数据集抽取 — 从生产 story.db（只读）+ 证据目录构建离线 gold 集。

数据源:
- DB:  ``C:/Users/zzh58/.story-lifecycle/story.db``（只读,绝不在抽取路径写该库）
- 证据: ``D:/hc-all/story/<前缀>-<slug>/``（hc-all 工作区）/ 仓库内 ``story/`` 与 ``.story/``
  （story-lifecycle 工作区）

输出: ``packages/eval/dataset/<story_key>/``（artifact 拷贝 + manifest.json）。
入选门槛: 至少 PRD + spec;``core=true`` = spec + plan + test-report 三者齐全。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("eval.dataset")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATASET_DIR = Path(os.environ.get("EVAL_DATASET_DIR") or (PACKAGE_ROOT / "dataset"))
DATASET_DIR = Path(os.environ.get("EVAL_DATASET_DIR") or DEFAULT_DATASET_DIR)

DEFAULT_DB = r"C:/Users/zzh58/.story-lifecycle/story.db"
HC_EVIDENCE_ROOT = Path("D:/hc-all/story")
HC_PRD_STORE = Path("D:/hc-all/prd")

# 要收集的文档 artifact 文件名 → manifest docs 键
DOC_FILES: dict[str, list[str]] = {
    "prd": ["PRD.md", "prd.md", "Prd.md"],
    "research": ["research.md", "Research.md"],
    "spec": ["spec.md", "Spec.md", "design.md"],
    "plan": ["plan.md", "Plan.md"],
    "test_report": ["test-report.md", "test_report.md", "test-report.md.meta"],
}
META_SUFFIX = ".meta"
OPTIONAL_FILES = ["ddl.sql", "ddl.md", "context-pack.md"]
GIT_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
BRANCH_RE = re.compile(r"(feature|release|hotfix|master|main)/[A-Za-z0-9_./\-]+")
DESIGN_FILE_RE = re.compile(r".*-design\.md$", re.IGNORECASE)

MAX_COPY_BYTES = 5 * 1024 * 1024  # 单文件拷贝上限 5MB


def _read_text_robust(path: Path, limit: int = 2_000_000) -> str:
    """读取文本,utf-8 → gbk 容错,超限截断。"""
    try:
        raw = path.read_bytes()[:limit]
    except OSError:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _safe_segment(value: str) -> str:
    """story_key → 安全路径段（story-lifecycle safe_segment 语义）。"""
    cleaned = re.sub(r"[^\w.-]+", "-", value or "", flags=re.UNICODE).strip("-_").rstrip(".")
    if "/" in cleaned or "\\" in cleaned or cleaned in {"..", "."}:
        raise ValueError(f"refusing unsafe path segment: {value!r}")
    return cleaned or "story"


def _numeric_prefixes(story_key: str) -> set[str]:
    """story_key 的所有数字段（首段 + 尾段）。

    历史证据目录两种命名都出现过:tapd-1144381896001065488 时代 → 16 位首段
    （``1144381896001065488-*``）;旧时代 → 尾段（``1065488-*``）。
    """
    runs = re.findall(r"\d+", story_key or "")
    return {runs[0], runs[-1]} if runs else set()


def _find_evidence_dirs(workspace: str, story_key: str) -> list[Path]:
    """按 workspace 映射证据根,按 `<数字前缀>-*` 匹配证据目录。"""
    prefixes = _numeric_prefixes(story_key)
    if not prefixes:
        return []
    ws_norm = (workspace or "").replace("\\", "/").rstrip("/")
    roots: list[Path] = []
    if ws_norm.startswith("D:/hc-all") or ws_norm == "D:/hc-all":
        roots.append(HC_EVIDENCE_ROOT)
    elif ws_norm in ("D:/github/story-lifecycle", ".") or ws_norm.endswith("/story-lifecycle"):
        roots.append(Path.cwd() / "story")
        roots.append(Path.cwd() / ".story")
    else:
        # 兜底:工作区同目录下疑似 story/ 目录
        ws_dir = Path(workspace) if workspace else Path.cwd()
        roots.append(ws_dir / "story")
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            for p in prefixes:
                if d.name == p or d.name.startswith(p + "-"):
                    found.append(d)
                    break
    # 同 key 多目录:按 mtime 升序,同名文件后写覆盖（合并时用）
    return sorted(found, key=lambda p: p.stat().st_mtime)


def _find_prd_store_files(story_key: str) -> list[Path]:
    """D:/hc-all/prd 仓里的 PRD 文件（旧年代命名:<id>*.md 或 *STORY-<id>*.md）。"""
    prefixes = _numeric_prefixes(story_key)
    if not prefixes or not HC_PRD_STORE.exists():
        return []
    hits: list[Path] = []
    for f in HC_PRD_STORE.iterdir():
        if f.suffix.lower() != ".md" or not f.is_file():
            continue
        name = f.stem
        for p in prefixes:
            if name == p or name.startswith(p) or f"-{p}" in name or f"_{p}" in name:
                hits.append(f)
                break
    return hits


def _collect_docs(evidence_dirs: list[Path], prd_store_files: list[Path] | None = None) -> tuple[dict[str, str], list[Path]]:
    """从证据目录收集 docs（后目录同名文件覆盖前目录）→ {doc_key: filename}。

    返回 (docs, copied_files)。每个 doc 键取存在的第一个候选文件名,
    文件内容按证据目录 mtime 顺序合并（后写覆盖）。spec 额外识别 ``*-design.md``
    （旧命名约定）;prd 额外兜底 prd 仓文件。
    """
    docs: dict[str, str] = {}
    seen_files: dict[str, Path] = {}  # 文件名 → 最新源路径
    for d in evidence_dirs:
        for f in d.iterdir():
            if f.is_file():
                seen_files[f.name] = f
    for doc_key, candidates in DOC_FILES.items():
        for cand in candidates:
            if cand in seen_files:
                docs[doc_key] = cand
                break
    if "spec" not in docs:
        for name in sorted(seen_files):
            if DESIGN_FILE_RE.match(name) and name not in DOC_FILES["spec"]:
                docs["spec"] = name
                break
    if "prd" not in docs and prd_store_files:
        docs["prd"] = prd_store_files[0].name
        seen_files.setdefault(prd_store_files[0].name, prd_store_files[0])
    return docs, sorted(seen_files.values())


def _collect_receipts(workspace: str, story_key: str) -> list[Path]:
    """收集 `<workspace>/.story/done/<key>/*.json` 阶段回执。"""
    variants = {_safe_segment(story_key)}
    runs = re.findall(r"\d+", story_key or "")
    if runs:
        variants.add(runs[-1])
    receipts: list[Path] = []
    for safe in variants:
        for suffix in ("", ".bak"):
            base = Path(workspace) / ".story" / "done" / (safe + suffix)
            if base.exists() and base.is_dir():
                receipts.extend(sorted(base.glob("*.json")))
    return receipts


def _extract_git(db: sqlite3.Connection, story_id: int) -> dict[str, Any]:
    """尽力而为挖 git 关联:branch_bound detail + change_item evidence_ref。"""
    branch: str | None = None
    commits: list[str] = []
    try:
        for r in db.execute(
            "SELECT detail FROM gate_result WHERE story_id=? AND gate_name='branch_bound'",
            (story_id,),
        ):
            detail = r[0] or ""
            try:
                obj = json.loads(detail)
                ev = obj.get("evidence") or {}
                b = ev.get("branch") if isinstance(ev, dict) else None
                if isinstance(b, list):
                    b = b[0] if b else None
                if b:
                    branch = str(b)
            except (json.JSONDecodeError, TypeError):
                pass
            if not branch:
                m = BRANCH_RE.search(detail)
                if m:
                    branch = m.group(0)
            commits.extend(GIT_HASH_RE.findall(detail))
    except sqlite3.Error as e:
        log.warning("branch_bound 读取失败: %s", e)
    return {"branch": branch, "commits": sorted(set(commits))}


def _extract_change_commits(db: sqlite3.Connection, story_key: str) -> list[str]:
    """story_change_item.evidence_ref 里的 commit hash。"""
    commits: list[str] = []
    try:
        for (ref,) in db.execute(
            "SELECT evidence_ref FROM story_change_item WHERE story_key=?", (story_key,)
        ):
            if ref:
                commits.extend(GIT_HASH_RE.findall(ref))
    except sqlite3.Error as e:
        log.warning("story_change_item 读取失败: %s", e)
    return sorted(set(commits))


def _extract_gates(db: sqlite3.Connection, story_id: int) -> list[dict]:
    try:
        rows = db.execute(
            "SELECT stage, gate_name, result FROM gate_result WHERE story_id=? ORDER BY id",
            (story_id,),
        ).fetchall()
        return [{"stage": r[0], "gate_name": r[1], "result": r[2]} for r in rows]
    except sqlite3.Error as e:
        log.warning("gate_result 读取失败: %s", e)
        return []


def _copy_files(files: list[Path], dest_dir: Path) -> list[str]:
    """拷贝证据文件（含 .meta）到 dest_dir,返回相对文件名列表。"""
    copied: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in files:
        try:
            if src.stat().st_size > MAX_COPY_BYTES:
                log.debug("跳过超大文件 %s", src)
                continue
            dst = dest_dir / src.name
            shutil.copy2(src, dst)
            copied.append(src.name)
        except OSError as e:
            log.warning("拷贝失败 %s: %s", src, e)
    return copied


@dataclass
class ExtractStats:
    entities_total: int = 0
    qualified: int = 0
    qualified_ab: int = 0
    qualified_c: int = 0
    core: int = 0
    core_with_diffs: int = 0
    errors: list[str] = field(default_factory=list)


def _load_entities() -> list[dict]:
    path = DATASET_DIR / "stories_matched.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            "stories_matched.jsonl 不存在——先跑 `eval link`"
        )
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _delivery_index() -> dict[tuple[str, str], dict]:
    """(repo, merge_hash) → delivery（commits/diffstat）。"""
    from .gitindex import load_deliveries

    return {(d["repo"], d["merge_hash"]): d for d in load_deliveries()}


def _gates_for_story(conn: sqlite3.Connection, story_key: str) -> list[dict]:
    if not story_key:
        return []
    row = conn.execute("SELECT id FROM story WHERE story_key=?", (story_key,)).fetchone()
    if not row:
        return []
    return _extract_gates(conn, row[0])


def _write_diff(repo_name: str, merge_hash: str, diff_dir: Path) -> dict:
    """core 集落 diff 全文（只读 git diff,超 5MB 跳过）。"""
    repo_path = Path("D:/hc-all") / repo_name
    if repo_name == "hc-admin":
        repo_path = Path("D:/hc-all/frontends/hc-admin")
    if not repo_path.exists():
        return {"error": "repo 不存在"}
    import subprocess

    try:
        r = subprocess.run(
            ["git", "--no-pager", "diff", f"{merge_hash}^1", merge_hash],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if r.returncode != 0:
            return {"error": r.stderr[:200]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    if len(r.stdout) > MAX_COPY_BYTES:
        return {"error": f"diff 过大({len(r.stdout)}B),跳过"}
    diff_dir.mkdir(parents=True, exist_ok=True)
    out = diff_dir / f"{repo_name}_{merge_hash}.diff"
    out.write_text(r.stdout, encoding="utf-8")
    return {"bytes": len(r.stdout)}


def extract(
    db_path: str | Path = DEFAULT_DB,
    dataset_dir: str | Path | None = None,
    workspace: str | None = None,
    story_key: str | None = None,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """以三方匹配实体为轴落盘评分材料（Task 2）。

    入选门槛: A∩B（有交付有需求）或 C 有 PRD+spec;
    ``core=true`` = spec+plan+test-report + ≥1 个 high/official 交付单元。
    """
    db_path = Path(db_path)
    ds_dir = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
    ds_dir.mkdir(parents=True, exist_ok=True)

    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    entities = _load_entities()
    if limit:
        entities = entities[:limit]
    deliveries = _delivery_index()

    stats = ExtractStats(entities_total=len(entities))
    manifests: list[dict[str, Any]] = []

    for ent in entities:
        tapd_id = ent.get("tapd_id") or ""
        sk = ent.get("story_key") or ""
        if story_key and sk != story_key:
            continue
        try:
            ab_links = [d for d in ent.get("deliveries", []) if d.get("confidence")]
            ab_high = any(
                d.get("confidence") in ("high", "official") for d in ent["deliveries"]
            )
            # C 文档
            docs: dict[str, str] = {}
            files: list[Path] = []
            evidence_dir = ent.get("evidence_dir") or ""
            if evidence_dir:
                ed = Path(evidence_dir)
                if ed.is_dir():
                    docs, files = _collect_docs([ed])
            if not docs.get("prd"):
                prd_store = _find_prd_store_files(sk or tapd_id)
                if prd_store:
                    docs["prd"] = prd_store[0].name
                    files.append(prd_store[0])
            c_ok = bool(docs.get("prd") and docs.get("spec"))
            # 入选门槛
            if not (ab_links and (c_ok or True)) and not c_ok:
                continue
            qualified = bool(ab_links or c_ok)
            stats.qualified += 1
            if ab_links:
                stats.qualified_ab += 1
            if c_ok:
                stats.qualified_c += 1

            # core = spec+plan C 文档 + ≥1 high/official 交付单元
            # （用户定案:放宽 test-report 硬性要求,见 docs/eval-build-handoff.md）
            core = bool(
                docs.get("spec") and docs.get("plan") and ab_high
            )
            if core:
                stats.core += 1

            # 输出目录
            dir_name = _safe_segment(tapd_id or sk or f"entity-{stats.qualified}")
            story_dir = ds_dir / dir_name
            if story_dir.exists():
                if not force:
                    log.info("跳过已存在 %s（--force 覆盖）", dir_name)
                    continue
                shutil.rmtree(story_dir, ignore_errors=True)
            story_dir.mkdir(parents=True, exist_ok=True)

            if files:
                _copy_files(files, story_dir)
            ws_lookup = ""
            if sk:
                row = conn.execute("SELECT workspace FROM story WHERE story_key=?", (sk,)).fetchone()
                ws_lookup = row[0] if row else ""
            receipts = _collect_receipts(ws_lookup or evidence_dir.split("/story/")[0], sk) if sk else []
            if receipts:
                rec_dir = story_dir / "receipts"
                rec_dir.mkdir(exist_ok=True)
                for rp in receipts:
                    shutil.copy2(rp, rec_dir / rp.name)

            # A 侧明细
            delivery_details = []
            for dl in ent["deliveries"]:
                full = deliveries.get((dl["repo"], dl["merge_hash"]), {})
                delivery_details.append(
                    {
                        "repo": dl["repo"],
                        "merge_hash": dl["merge_hash"],
                        "branch": dl.get("branch", ""),
                        "link_method": dl.get("link_method", ""),
                        "confidence": dl.get("confidence", ""),
                        "merged_at": full.get("merged_at", ""),
                        "author": full.get("author", ""),
                        "commits": [
                            {"hash": c.get("hash", ""), "subject": c.get("subject", "")[:200]}
                            for c in full.get("commits", [])[:50]
                        ],
                        "diffstat": full.get("diffstat", {}),
                    }
                )
            # core 集落 diff 全文
            diff_result = {}
            if core:
                diff_dir = story_dir / "diffs"
                for dl in ent["deliveries"]:
                    if dl.get("confidence") not in ("high", "official"):
                        continue
                    r = _write_diff(dl["repo"], dl["merge_hash"], diff_dir)
                    diff_result[f"{dl['repo']}_{dl['merge_hash'][:10]}"] = r
                    if r.get("bytes"):
                        stats.core_with_diffs += 1

            manifest = {
                "tapd_id": tapd_id,
                "story_key": sk,
                "dir": dir_name,
                "title": ent.get("name") or ent.get("story_title") or "",
                "tapd_status": ent.get("status", ""),
                "iteration_id": ent.get("iteration_id", ""),
                "owner": ent.get("owner", ""),
                "evidence_dir": evidence_dir,
                "link_summary": ent.get("link_summary", {}),
                "link_notes": ent.get("link_notes", []),
                "docs": docs,
                "gates": _gates_for_story(conn, sk),
                "deliveries": delivery_details,
                "diffs": diff_result,
                "core": core,
            }
            with open(story_dir / "manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            manifests.append(manifest)
            log.info(
                "[%s] qualified(ab=%s c=%s) core=%s docs=%s deliveries=%d",
                dir_name,
                bool(ab_links),
                c_ok,
                core,
                list(docs.keys()),
                len(delivery_details),
            )
        except Exception as e:  # noqa: BLE001 — 单实体失败不中断
            stats.errors.append(f"{tapd_id or sk}: {e}")
            log.exception("实体 %s 抽取失败", tapd_id or sk)

    conn.close()
    summary = {
        "entities_total": stats.entities_total,
        "qualified": stats.qualified,
        "qualified_ab": stats.qualified_ab,
        "qualified_c": stats.qualified_c,
        "core": stats.core,
        "core_with_diffs": stats.core_with_diffs,
        "errors": stats.errors,
        "dataset_dir": str(ds_dir),
    }
    with open(ds_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def load_manifests(dataset_dir: str | Path | None = None, core_only: bool = False) -> list[dict]:
    """读取已抽取 manifest 列表。"""
    ds_dir = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
    manifests: list[dict] = []
    if not ds_dir.exists():
        return manifests
    for story_dir in sorted(ds_dir.iterdir()):
        if not story_dir.is_dir():
            continue
        mf = story_dir / "manifest.json"
        if not mf.exists():
            continue
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if core_only and not manifest.get("core"):
            continue
        manifests.append(manifest)
    return manifests


def artifact_path(dataset_dir: Path, manifest: dict, doc_key: str) -> Path | None:
    """manifest 中某 doc_key 对应的文件路径。"""
    fn = (manifest.get("docs") or {}).get(doc_key)
    if not fn:
        return None
    d = manifest.get("dir") or _safe_segment(manifest.get("story_key") or manifest.get("tapd_id") or "story")
    p = Path(dataset_dir) / d / fn
    return p if p.exists() else None
