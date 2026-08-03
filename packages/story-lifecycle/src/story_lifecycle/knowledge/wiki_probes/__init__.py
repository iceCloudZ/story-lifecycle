"""config 驱动的 wiki probe 加载(mirror verify_providers/__init__.py)。

config ``wiki_probes: [{module, class, path?, ...}]`` 驱动 importlib:
- 未配置 → 返回空列表(零配置 = 只有核心自带 L1 CodeScanProbe)
- 单条失败 → print + 跳过,不阻断其它 probe(§5.2 容错)
- duck-type 校验只查 probe() 方法(同 R6 哲学,hc 侧不必硬装本包)
"""

from __future__ import annotations

import importlib
import logging
import sys

from .base import BaseWikiProbe

log = logging.getLogger("story-lifecycle.wiki_probes")


def load_wiki_probes(config: dict) -> list[BaseWikiProbe]:
    """从 config 加载所有 wiki probe。任何失败单独降级(跳过该条),绝不抛。"""
    probes: list[BaseWikiProbe] = []
    for cfg in config.get("wiki_probes") or []:
        try:
            if cfg.get("path"):
                p = cfg["path"]
                if p not in sys.path:
                    sys.path.insert(0, p)
            module = importlib.import_module(cfg["module"])
            cls = getattr(module, cfg["class"])
            if not callable(getattr(cls, "probe", None)):
                raise TypeError(f"{cfg['class']} 缺少 probe() 方法(duck-type 校验)")
            probes.append(cls(config=cfg))
        except Exception as e:  # noqa: BLE001 — 容错:单条失败跳过,不阻断其它 probe
            log.warning("[wiki_probe] 加载失败,跳过: %s: %s", cfg.get("class"), e)
    return probes
