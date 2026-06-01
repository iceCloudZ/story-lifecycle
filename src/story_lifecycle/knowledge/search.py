"""知识包结构化搜索 — 最小版本。"""

from __future__ import annotations

import re
from pathlib import Path

from .paths import knowledge_dir

_TYPE_PATHS: dict[str, list[str]] = {
    "api": ["indexes/api-index.md"],
    "table": ["indexes/table-index.md"],
    "field": ["indexes/field-index.md"],
    "mq": ["indexes/mq-index.md"],
    "service": ["indexes/service-index.md"],
    "scenario": ["scenarios/"],
    "state_machine": ["indexes/state-machine-index.md"],
    "enum": ["indexes/enum-index.md"],
    "bug": ["indexes/bug-risk-index.md"],
    "test_case": ["indexes/test-case-index.md"],
    "text": [],
}


def search_knowledge(
    workspace: str | Path,
    keyword: str,
    target_type: str = "text",
    limit: int = 20,
) -> list[dict]:
    """在知识包中搜索关键词。

    Args:
        workspace: 项目工作区路径
        keyword: 搜索关键词（自动转义正则特殊字符）
        target_type: 限制搜索的索引类型
        limit: 最大返回条目数

    Returns:
        [{"file": str, "line": str, "line_no": int}]
    """
    root = knowledge_dir(workspace)
    if not root.exists():
        return []

    search_paths = _resolve_paths(root, target_type)
    pattern = re.escape(keyword)

    results: list[dict] = []
    for sp in search_paths:
        if sp.is_dir():
            results.extend(_search_dir(sp, pattern, limit - len(results)))
        elif sp.exists():
            results.extend(_search_file(sp, pattern, limit - len(results)))
        if len(results) >= limit:
            break

    return results[:limit]


def _resolve_paths(root: Path, target_type: str) -> list[Path]:
    if target_type == "text":
        return [root]
    rel_paths = _TYPE_PATHS.get(target_type, [])
    paths = []
    for rp in rel_paths:
        p = root / rp
        if p.exists():
            paths.append(p)
    if not paths:
        paths = [root]
    return paths


def _search_file(path: Path, pattern: str, limit: int) -> list[dict]:
    results = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return results
    rel = str(path)
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line, re.IGNORECASE):
            results.append({"file": rel, "line": line.strip(), "line_no": i})
            if len(results) >= limit:
                break
    return results


def _search_dir(dirpath: Path, pattern: str, limit: int) -> list[dict]:
    results = []
    for f in dirpath.rglob("*.md"):
        results.extend(_search_file(f, pattern, limit - len(results)))
        if len(results) >= limit:
            break
    return results
