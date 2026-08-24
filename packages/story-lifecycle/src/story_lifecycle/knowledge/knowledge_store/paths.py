"""Path helpers for .story/knowledge/ layout.

All runtime code must use these helpers instead of hand-building paths.

    .story/knowledge/
      product.yaml
      manifest.yaml
      search-catalog.md
      scenarios/<domain>/
      indexes/*.md
      indexes/by-domain/<domain>.md
      graph/product-context-graph.json
      playbooks/
      declarations/
      reviews/
      events/
      cache/
"""

from __future__ import annotations

import os
from pathlib import Path

from ...infra.story_paths import safe_story_path

# 全局默认知识根(未配置、workspace 无本地知识库时的最终回退)。
# 保持与历史 STORY_KNOWLEDGE_ROOT 默认值一致。
_GLOBAL_DEFAULT_KNOWLEDGE_ROOT = Path(
    "D:/hc-all/.story/knowledge" if os.name == "nt" else "~/hc-all/.story/knowledge"
).expanduser()


def resolve_knowledge_root(workspace: str | Path | None) -> Path:
    """知识库根的唯一解析入口 —— 读侧(provider)与写侧(reflection)必须共用。

    飞轮断点 B4 的修复不变量:任何新增知识写入/读取点都经此函数,
    禁止再出现模块级硬编码路径常量。解析顺序:

    1. ``config.yaml`` 的 ``knowledge_root``(显式配置,最高优先)
    2. env ``STORY_KNOWLEDGE_ROOT``
    3. ``<workspace>/.story/knowledge`` 且已初始化(含 manifest.yaml 或 INDEX.json)
    4. 全局默认(单机单知识库语义;workspace 未建知识库时沉淀/召回都落到它)
    """
    try:
        from ...infra.config import get_config

        configured = (get_config() or {}).get("knowledge_root")
        if configured:
            return Path(configured).expanduser()
    except Exception:  # noqa: BLE001 — 配置读取失败不阻塞解析
        pass

    env = os.environ.get("STORY_KNOWLEDGE_ROOT")
    if env:
        return Path(env).expanduser()

    if workspace:
        local = knowledge_dir(workspace)
        if (local / "manifest.yaml").exists() or (local / "INDEX.json").exists():
            return local

    return _GLOBAL_DEFAULT_KNOWLEDGE_ROOT


def knowledge_dir(workspace: str | Path) -> Path:
    return Path(workspace) / ".story" / "knowledge"


def manifest_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "manifest.yaml"


def product_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "product.yaml"


def search_catalog_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "search-catalog.md"


def graph_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "graph"


def graph_json_path(workspace: str | Path) -> Path:
    return graph_dir(workspace) / "product-context-graph.json"


def scenarios_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "scenarios"


def indexes_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "indexes"


def index_by_domain_dir(workspace: str | Path) -> Path:
    return indexes_dir(workspace) / "by-domain"


def playbooks_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "playbooks"


def declarations_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "declarations"


def reviews_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "reviews"


def events_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "events"


def cache_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "cache"


def knowledge_done_file(workspace: str | Path) -> Path:
    """Done file for the PROJECT-KNOWLEDGE-INIT bootstrap."""
    return (
        Path(workspace)
        / ".story"
        / "done"
        / "PROJECT-KNOWLEDGE-INIT"
        / "knowledge_bootstrap.json"
    )


def knowledge_context_dir(workspace: str | Path, story_key: str) -> Path:
    return safe_story_path(
        workspace, ".story", "context", story_key, "knowledge-context"
    )


def runs_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "runs"


def run_dir(workspace: str | Path, run_id: str) -> Path:
    return runs_dir(workspace) / run_id
