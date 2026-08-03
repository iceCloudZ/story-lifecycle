"""config 驱动的 verify provider 加载（mirror context_providers）。

对应设计：docs/project-intelligence/10-test-framework-integration-design.md 改动 1.2。
修订点 R6：duck-type 校验（只要求有 verify() 方法），不强制
issubclass(BaseVerifyProvider)——hc 侧实现因此不必在运行环境硬装
story-lifecycle 包，跨仓依赖降为纯协议。
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Optional

from .base import BaseVerifyProvider

log = logging.getLogger("story-lifecycle.verify_providers")


def load_verify_provider(config: dict) -> Optional[BaseVerifyProvider]:
    """从 config 加载 verify provider。未配置返回 None。

    任何失败（缺配置/import 错误/provider 实例化异常）都返回 None——
    降级到 LLM-only gate，绝不阻断 story 流程（同 context_providers 容错哲学）。
    """
    cfg = config.get("verify_provider")
    if not cfg:
        return None
    try:
        # 可选 sys.path prepend（加载非已安装包，如 hc-pytest 的 provider 入口）
        if cfg.get("path"):
            p = cfg["path"]
            if p not in sys.path:
                sys.path.insert(0, p)
        module = importlib.import_module(cfg["module"])
        cls = getattr(module, cfg["class"])
        if not callable(getattr(cls, "verify", None)):
            raise TypeError(f"{cfg['class']} 缺少 verify() 方法（duck-type 校验）")
        return cls(config=cfg)
    except Exception as e:  # noqa: BLE001 — 容错：加载失败不阻断，降级到 LLM-only gate
        log.warning("[verify_provider] 加载失败，降级到 LLM gate: %s", e)
        return None
