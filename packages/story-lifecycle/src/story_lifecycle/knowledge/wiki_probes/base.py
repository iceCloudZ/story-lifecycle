"""wiki 探测源契约(11-workspace-entity-design.md §5.2 — 第六条缝)。

核心不硬依赖任何特定数据源(DMS/SLS/ES/Mongo)。失败返回 [] 不阻断
(同 BaseStoryContextProvider 容错哲学)。

先例同构:context_provider / source / adapter / verify_provider(文档 10)/
knowledge Provider。加载器见 ``wiki_probes/__init__.py``。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Evidence:
    """一次探测产出的一条证据。只含聚合统计,绝无原始行(I5 PII 红线)。"""

    layer: str  # L1 | L2 | L3 | L4
    kind: str  # table_distribution | api_traffic | api_endpoints | ...
    summary: str  # 人读的一句话结论
    data: dict = field(default_factory=dict)  # 聚合统计(计数/分布/比例)
    query: str = ""  # 产生此证据的查询(审计 + 复跑)
    observed_at: str = ""


class BaseWikiProbe(ABC):
    """wiki 探测源抽象。子类实现 probe() 即可接入(duck-type 校验同 verify_provider)。"""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def probe(self, workspace: dict) -> list[Evidence]:
        """执行探测,返回证据列表。异常/未配置 → 返回 [],不抛。"""
        ...
