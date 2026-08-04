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
DEFAULT_DATASET_DIR = PACKAGE_ROOT / "dataset"

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
    total_completed: int = 0
    with_evidence: int = 0
    qualified: int = 0
    core: int = 0
    skipped_no_evidence: int = 0
    skipped_threshold: int = 0
    fs_only: int = 0
    errors: list[str] = field(default_factory=list)


def _extract_filesystem_only(
    ds_dir: Path, db_keys: set[str], all_db_keys: set[str], manifests: list[dict], force: bool
) -> int:
    """旧时代 story（DB 已删除）:证据目录 + PRD 仓齐全即入选。

    只收数字前缀 ≥6 位的目录（排除测试目录 2-* 等）;PRD 优先目录内,缺失则
    从 ``D:/hc-all/prd`` 仓兜底。前缀命中任何 DB story（含 failed/已删除）的
    目录不收——它们不是「人工验收过的完成 story」。manifest 带
    ``source: filesystem`` 标记。
    """
    added = 0
    if not HC_EVIDENCE_ROOT.exists():
        return 0
    for d in sorted(HC_EVIDENCE_ROOT.iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r"(\d{6,})", d.name)
        if not m:
            continue
        prefix = m.group(1)
        if any(k.startswith(prefix) or k.endswith(prefix) for k in all_db_keys):
            continue
        docs, files = _collect_docs([d])
        if not docs.get("spec"):
            continue
        if not docs.get("prd"):
            prd_store = _find_prd_store_files(prefix)
            if not prd_store:
                continue
            docs["prd"] = prd_store[0].name
            files.append(prd_store[0])
        story_key = f"fs-{prefix}"
        story_dir = ds_dir / story_key
        if story_dir.exists():
            if not force:
                continue
            shutil.rmtree(story_dir, ignore_errors=True)
        story_dir.mkdir(parents=True, exist_ok=True)
        _copy_files(files, story_dir)
        manifest = {
            "story_key": story_key,
            "title": d.name.split("-", 1)[1] if "-" in d.name else d.name,
            "workspace": str(HC_EVIDENCE_ROOT),
            "profile": "",
            "completed_at": "",
            "docs": docs,
            "git": {"branch": None, "commits": []},
            "gates": [],
            "core": bool(docs.get("plan") and docs.get("test_report")),
            "source": "filesystem",
        }
        with open(story_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        manifests.append(manifest)
        added += 1
        log.info("[%s] filesystem-only core=%s docs=%s", story_key, manifest["core"], list(docs.keys()))
    return added


def extract(
    db_path: str | Path = DEFAULT_DB,
    dataset_dir: str | Path | None = None,
    workspace: str | None = None,
    story_key: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """执行抽取,返回统计摘要 dict。

    Args:
        workspace: 限定单个 workspace（调试用）。
        story_key: 限定单个 story_key（调试用）。
        force: 覆盖已存在目录。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"DB 不存在: {db_path}")
    ds_dir = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
    ds_dir.mkdir(parents=True, exist_ok=True)

    # 只读连接（绝不让抽取路径写生产库）
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    stats = ExtractStats()
    manifests: list[dict[str, Any]] = []

    sql = (
        "SELECT id, story_key, title, workspace, profile, updated_at FROM story "
        "WHERE status='completed' AND (is_test=0 OR is_test IS NULL) AND deleted_at IS NULL"
    )
    params: tuple = ()
    if story_key:
        sql += " AND story_key=?"
        params = (story_key,)
    rows = conn.execute(sql, params).fetchall()
    stats.total_completed = len(rows)

    for row in rows:
        d = dict(row)
        key = d["story_key"]
        ws = d.get("workspace") or ""
        if workspace and ws.replace("\\", "/") != workspace.replace("\\", "/"):
            continue
        try:
            evidence_dirs = _find_evidence_dirs(ws, key)
            prd_store = _find_prd_store_files(key)
            if not evidence_dirs and not prd_store:
                stats.skipped_no_evidence += 1
                continue
            stats.with_evidence += 1
            docs, files = _collect_docs(evidence_dirs, prd_store)
            prd = docs.get("prd")
            spec = docs.get("spec")
            if not (prd and spec):
                stats.skipped_threshold += 1
                continue
            stats.qualified += 1

            story_dir = ds_dir / _safe_segment(key)
            if story_dir.exists():
                if not force:
                    log.info("跳过已存在 %s（--force 覆盖）", key)
                    continue
                shutil.rmtree(story_dir, ignore_errors=True)
            story_dir.mkdir(parents=True, exist_ok=True)

            copied = _copy_files(files, story_dir)
            receipts = _collect_receipts(ws, key)
            if receipts:
                rec_dir = story_dir / "receipts"
                rec_dir.mkdir(exist_ok=True)
                for rp in receipts:
                    shutil.copy2(rp, rec_dir / rp.name)

            git = _extract_git(conn, d["id"])
            git["commits"] = sorted(set(git["commits"] + _extract_change_commits(conn, key)))
            gates = _extract_gates(conn, d["id"])

            plan = docs.get("plan")
            test_report = docs.get("test_report")
            manifest = {
                "story_key": key,
                "title": d.get("title") or "",
                "workspace": ws,
                "profile": d.get("profile") or "",
                "completed_at": d.get("updated_at") or "",
                "docs": docs,
                "git": git,
                "gates": gates,
                "core": bool(plan and test_report),
            }
            if manifest["core"]:
                stats.core += 1
            with open(story_dir / "manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            manifests.append(manifest)
            log.info(
                "[%s] core=%s docs=%s receipts=%d",
                key,
                manifest["core"],
                list(docs.keys()),
                len(receipts),
            )
        except Exception as e:  # noqa: BLE001 — 单 story 失败不中断全量
            stats.errors.append(f"{key}: {e}")
            log.exception("story %s 抽取失败", key)

    # 文件系统-only 旧时代 story（DB 已无记录,但证据目录 + PRD 仓齐全）
    db_keys = {r["story_key"] for r in rows}
    all_db_keys = {r[0] for r in conn.execute("SELECT story_key FROM story")}
    stats.fs_only = _extract_filesystem_only(ds_dir, db_keys, all_db_keys, manifests, force)

    conn.close()

    summary = {
        "total_completed": stats.total_completed,
        "with_evidence": stats.with_evidence,
        "qualified": stats.qualified,
        "core": stats.core,
        "fs_only": stats.fs_only,
        "skipped_no_evidence": stats.skipped_no_evidence,
        "skipped_threshold": stats.skipped_threshold,
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
    p = dataset_dir / _safe_segment(manifest["story_key"]) / fn
    return p if p.exists() else None
