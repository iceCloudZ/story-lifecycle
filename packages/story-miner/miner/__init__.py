"""transcript 持久化与挖掘包。import 本包即触发所有 adapter 注册。"""
from .base import REGISTRY, SourceAdapter, register_adapter
from . import adapters  # 触发各端 adapter 的 @register_adapter

__all__ = ['REGISTRY', 'SourceAdapter', 'register_adapter']
