"""RUN 跑测文档新坑摄入(WP-F §8.1 写侧)。

解析 ``docs/test-runs/RUN-*.md`` 的「本轮新坑(候选回写)」表,逐行转
:class:`knowledge.models.FailureEntry` 落到 ``<knowledge_root>/failures/
failure-knowledge.json``(knowledge 包既有 failure 持久化约定:
``generator._collect_entries`` 扫它进 INDEX.json),写后刷新 INDEX.json。

解析规则(对着真实 RUN 文件校准):

- **story key**:标题行 ``# RUN — <key简写> (<日期>)`` 取第一个空白/括号前的
  token(如 ``tapd-1069389``);兼容 ``·``/``-`` 分隔与全角括号日期。解析不到用
  文件名 stem 兜底(保证 id 仍稳定)。
- **小节标题**:任何含「新坑」的标题(实测变体:「本轮新坑(候选回写)」/
  「本轮新坑」/「新坑与经验(已回写 skill RUNS.md)」/「新坑(本轮实证,未在
  速查表)」/「新坑 → 速查」);另认加粗列表行变体(「- **新坑入库**：」,
  RUN-tapd-1069471 实测——坑表缩进跟在列表项下,无 # 标题;仅当坑表表头
  真的紧随其后才认,防「见上文」类无表加粗行误开小节)。
- **表**:表头首列含「坑」的两列表(``| 坑 | 规则 |`` 与 ``| 坑/经验 | 处理 |``
  两种实测列形);另兼容「新坑 → 速查」的编号列表行(首个冒号切 坑/规则)。
- **容错**:缺列/空单元格/无冒号的行 skip + ``logging.warning``,绝不让整批
  导入失败(设计:malformed rows never fail the batch)。
- **幂等**:id 由 (story_key + 坑标题) hash 派生 —— 重复 import 同 id 原地更新,
  不产生重复条目。

所有写入经 :func:`resolve_knowledge_root`(显式 ``root`` 参数覆盖),遵守
「写读同根」不变量(paths.py B4)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .paths import resolve_knowledge_root

logger = logging.getLogger(__name__)

# FailureEntry.category 固定值(检索/统计按它认领 RUN 新坑条目)
PITFALL_CATEGORY = "run-pitfall"

# 持久化文件(knowledge 包 generator._collect_entries 的 failure 扫描目标)
_FAILURES_REL = os.path.join("failures", "failure-knowledge.json")

# 标题行:# RUN — tapd-1069389 (2026-09-08) / # RUN · tapd-xxx 标题(2026-09-10)
_RUN_TITLE_RE = re.compile(r"^#\s+RUN\s*[—·\-–]?\s*(.+?)\s*$", re.MULTILINE)
# 编号列表行:「新坑 → 速查」变体,如 `1. **yorkie 钩子必炸**:原因...`
_NUM_LIST_RE = re.compile(r"^(\d+)[.、)]\s+(.+)$")
# 加粗列表行小节变体:`- **新坑入库**：`(无 # 标题,坑表缩进跟在列表项下)。
# 单独出现不算小节开头——还要坑表表头紧随其后(见 _pitfall_table_header_nearby)。
_BOLD_BULLET_XINKENG_RE = re.compile(r"^\s*-\s*\*\*[^*]*新坑[^*]*\*\*")
# 表分隔行:|---|---| / | :--- | ---: |
_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")


def extract_run_story_key(md_text: str) -> str:
    """从 RUN md 的标题行提取 story key 简写(如 ``tapd-1069389``)。

    ``# RUN — <key> (日期)`` → 取日期括号/空白前的第一个 token;解析不到返回
    空串(调用方用文件名 stem 兜底)。
    """
    m = _RUN_TITLE_RE.search(md_text or "")
    if not m:
        return ""
    head = re.split(r"[\s(（]", m.group(1), maxsplit=1)[0]
    return head.strip("`*").strip()


def pitfall_entry_id(story_key: str, title: str) -> str:
    """(story_key + 坑标题) hash 派生稳定 id —— 重复导入幂等的关键。"""
    raw = f"{story_key}\x00{title}".encode("utf-8")
    return f"failure:run-pitfall-{hashlib.sha1(raw).hexdigest()[:12]}"


def _clean_cell(text: str) -> str:
    """去掉单元格里的 markdown 加粗/行内码包裹,保留正文。"""
    return text.replace("**", "").replace("__", "").strip()


def _split_table_row(line: str) -> list[str]:
    """``| a | b |`` → ``["a", "b"]``(不去 markdown 标记,分层处理)。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        _SEPARATOR_CELL_RE.fullmatch(c) for c in cells if c
    ) and any(c for c in cells)


def _pitfall_table_header_nearby(lines: list[str], start: int, window: int = 3) -> bool:
    """lines[start] 起 window 行内是否出现坑表表头(首列含「坑」且 ≥2 列)。

    加粗列表行小节的误触发防线:只有坑表真的紧随其后才认小节开头
    (「- **新坑复盘**：见上文」这类无表行不开小节)。判据与
    parse_run_pitfall_rows 的 header_ok 完全一致;空行占 window 名额,
    实测形态(表头在加粗行的下一行)远在窗口内。
    """
    for line in lines[start : start + window]:
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = _split_table_row(s)
        if len(cells) >= 2 and "坑" in cells[0]:
            return True
    return False


def _pitfall_sections(md_text: str) -> list[tuple[int, list[str]]]:
    """按行扫出所有含「新坑」的小节 → [(标题级别, 小节行), ...]。

    两种小节开头:含「新坑」的标题行(#..######,原行为);或含「新坑」的
    加粗列表行(如 ``- **新坑入库**：``,RUN-tapd-1069471 实测变体)——后者
    仅当坑表表头紧随其后(_pitfall_table_header_nearby)才认。

    小节在下一个同级或更高级标题处结束(更深的子标题仍算本节)。加粗列表行
    小节级别取 7(深于一切标题)→ 后续任意标题都终结它;已在小节内时加粗行
    不重复开节(表照常被外层小节采集,行不重不漏)。
    """
    sections: list[tuple[int, list[str]]] = []
    current: tuple[int, list[str]] | None = None
    lines = (md_text or "").splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+", line)
        if m:
            level = len(m.group(1))
            if current is not None and level <= current[0]:
                sections.append(current)
                current = None
            if current is None and "新坑" in line:
                current = (level, [])
                continue
        elif current is None and _BOLD_BULLET_XINKENG_RE.match(line) and _pitfall_table_header_nearby(lines, i + 1):
            current = (7, [])
            continue
        if current is not None:
            current[1].append(line)
    if current is not None:
        sections.append(current)
    return sections


def parse_run_pitfall_rows(md_text: str, source: str = "") -> tuple[list[tuple[str, str]], int]:
    """解析 RUN md 里的 (坑, 规则) 行。

    返回 ``(rows, skipped)``:rows 是清洗后的二元组列表;skipped 是因格式
    不对被跳过并打了 warning 的行数(缺列/空单元格/编号行无冒号)。
    """
    rows: list[tuple[str, str]] = []
    skipped = 0

    def _warn(reason: str, line: str) -> None:
        nonlocal skipped
        skipped += 1
        logger.warning(
            "run_pitfalls: 跳过格式错误的行(%s)%s:%r", reason, f" 文件 {source}" if source else "", line
        )

    for _level, body_lines in _pitfall_sections(md_text or ""):
        header_seen = False
        header_ok = False
        for line in body_lines:
            s = line.strip()
            if s.startswith("|"):
                cells = _split_table_row(s)
                if not header_seen:
                    header_seen = True
                    header_ok = len(cells) >= 2 and "坑" in cells[0]
                    if not header_ok:
                        _warn("表头首列不含「坑」", line)
                    continue
                if _is_separator_row(cells):
                    continue
                if not header_ok:
                    continue
                if len(cells) != 2 or not cells[0] or not cells[1]:
                    _warn("坑表行需恰好两列(坑|规则)且非空", line)
                    continue
                rows.append((_clean_cell(cells[0]), _clean_cell(cells[1])))
                continue

            # 非表行:重置表状态(同节后续可能还有第二张表)
            header_seen = False
            header_ok = False

            m = _NUM_LIST_RE.match(s)
            if not m:
                continue
            text = _clean_cell(m.group(2))
            for sep in ("：", ":"):
                if sep in text:
                    title, detail = text.split(sep, 1)
                    break
            else:
                _warn("编号行缺「：」分隔(坑:规则)", line)
                continue
            title, detail = title.strip(), detail.strip()
            if not title or not detail:
                _warn("编号行的坑/规则有空侧", line)
                continue
            rows.append((title, detail))

    return rows, skipped


def _knowledge_deps():
    """软 seam:knowledge 包按需导入(standalone 运行时给中文报错)。"""
    try:
        from knowledge.generator import write_index
        from knowledge.models import FailureEntry
    except ImportError as exc:  # pragma: no cover - monorepo 外裸装才触发
        raise ImportError(
            "knowledge 包不可用 —— monorepo 里请先 `pip install -e packages/knowledge`"
        ) from exc
    return FailureEntry, write_index


def parse_run_pitfalls(md_text: str, source_path: str | os.PathLike) -> list:
    """RUN md 全文 → FailureEntry 列表(category="run-pitfall")。

    - title=坑,detail=规则,tags=[story_key, "run-pitfall"]
    - source_refs=[RUN md 绝对路径],id 幂等(见 :func:`pitfall_entry_id`)
    """
    FailureEntry, _ = _knowledge_deps()
    src = str(Path(source_path).resolve())
    story_key = extract_run_story_key(md_text) or Path(source_path).stem
    rows, _skipped = parse_run_pitfall_rows(md_text, source=str(source_path))
    tags = [t for t in (story_key, PITFALL_CATEGORY) if t]
    return [
        FailureEntry(
            id=pitfall_entry_id(story_key, title),
            type="failure",
            title=title,
            source="dynamic",
            category=PITFALL_CATEGORY,
            display_category=PITFALL_CATEGORY,
            detail=detail,
            tags=list(tags),
            source_refs=[src],
            path=_FAILURES_REL,
        )
        for title, detail in rows
    ]


def _load_failures_payload(failures_path: Path) -> dict:
    """读 failures/failure-knowledge.json(坏文件当空,merge_attribution_reports 同款)。"""
    if not failures_path.exists():
        return {"version": 1, "failures": []}
    try:
        payload = json.loads(failures_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("run_pitfalls: failures 文件读不了,当空处理:%s", failures_path)
        return {"version": 1, "failures": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("failures"), list):
        return {"version": 1, "failures": []}
    return payload


_UPDATABLE_FIELDS = (
    "title",
    "category",
    "display_category",
    "detail",
    "tags",
    "source_refs",
    "path",
)


def _upsert_entries(payload: dict, entries: list) -> tuple[int, int]:
    """按 id 原地更新/追加。返回 (imported, updated)。"""
    now = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    by_id = {f.get("id"): f for f in payload["failures"] if isinstance(f, dict)}
    imported = updated = 0
    for entry in entries:
        data = entry.to_dict()
        existing = by_id.get(entry.id)
        if existing is not None:
            for field in _UPDATABLE_FIELDS:
                existing[field] = data.get(field)
            existing["updated_at"] = now
            updated += 1
        else:
            data["created_at"] = now
            data["updated_at"] = now
            payload["failures"].append(data)
            by_id[entry.id] = data
            imported += 1
    return imported, updated


def _collect_md_files(target: str | os.PathLike) -> list[Path]:
    """单文件或目录(递归扫 *.md,排序保证输出稳定)。"""
    p = Path(target)
    if p.is_dir():
        return sorted(p.rglob("*.md"))
    if p.is_file():
        return [p]
    raise FileNotFoundError(f"路径不存在:{p}")


def import_run_pitfalls(
    target: str | os.PathLike, root: str | os.PathLike | None = None
) -> dict:
    """把一个 RUN md(或目录下全部 *.md)的新坑表导入知识库。

    root 缺省走 :func:`resolve_knowledge_root`(config/env/workspace 链)。
    返回::

        {"files": [{"file": str, "imported": int, "updated": int, "skipped": int}],
         "imported": int, "updated": int, "skipped": int,
         "failures_path": str, "index_path": str}

    幂等:同文件重复导入 → 全部走 update 路径,条目总数不变。
    """
    _FailureEntry, write_index = _knowledge_deps()  # 顺带校验 knowledge 包可用
    kroot = Path(root) if root else Path(resolve_knowledge_root(None))
    failures_path = kroot / _FAILURES_REL
    payload = _load_failures_payload(failures_path)

    file_reports: list[dict] = []
    total = {"imported": 0, "updated": 0, "skipped": 0}
    for md in _collect_md_files(target):
        try:
            md_text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("run_pitfalls: 跳过读不了的文件 %s:%s", md, exc)
            file_reports.append(
                {"file": str(md), "imported": 0, "updated": 0, "skipped": 0, "error": str(exc)}
            )
            continue
        rows, skipped = parse_run_pitfall_rows(md_text, source=str(md))
        entries = parse_run_pitfalls(md_text, source_path=md)
        imported, updated = _upsert_entries(payload, entries)
        file_reports.append(
            {"file": str(md), "imported": imported, "updated": updated, "skipped": skipped}
        )
        total["imported"] += imported
        total["updated"] += updated
        total["skipped"] += skipped

    # 写回 failures 文件 + 刷新 INDEX(无变化时重写内容相同,幂等无害)
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    failures_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    kroot.mkdir(parents=True, exist_ok=True)
    index_path = write_index(str(kroot))

    return {
        "files": file_reports,
        **total,
        "failures_path": str(failures_path),
        "index_path": str(index_path),
    }
