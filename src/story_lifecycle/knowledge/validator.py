# src/story_lifecycle/knowledge/validator.py
"""Validate .story/knowledge/ artifacts meet minimum standards."""

from __future__ import annotations

import json
from pathlib import Path

from .paths import (
    manifest_path,
    product_path,
    search_catalog_path,
    graph_json_path,
    scenarios_dir,
    indexes_dir,
)


def validate_knowledge_pack(workspace: str | Path) -> list[str]:
    """Return list of errors. Empty list = valid."""
    errors: list[str] = []

    mp = manifest_path(workspace)
    if not mp.exists():
        errors.append("manifest.yaml 不存在")
    else:
        try:
            import yaml

            data = yaml.safe_load(mp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append("manifest.yaml 格式错误：不是 YAML dict")
        except Exception as e:
            errors.append(f"manifest.yaml 解析失败: {e}")

    pp = product_path(workspace)
    if not pp.exists():
        errors.append("product.yaml 不存在")

    sc = search_catalog_path(workspace)
    if not sc.exists():
        errors.append("search-catalog.md 不存在")

    gp = graph_json_path(workspace)
    if not gp.exists():
        errors.append("graph/product-context-graph.json 不存在")
    else:
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append("graph JSON 格式错误：不是 JSON object")
        except json.JSONDecodeError as e:
            errors.append(f"graph JSON 解析失败: {e}")

    sd = scenarios_dir(workspace)
    if sd.exists():
        domains = [d for d in sd.iterdir() if d.is_dir()]
        if not domains:
            errors.append("警告：scenarios/ 下没有业务域目录")

    idx = indexes_dir(workspace)
    if idx.exists():
        md_files = list(idx.glob("*.md"))
        if not md_files:
            errors.append("警告：indexes/ 下没有索引文件")

    return errors
