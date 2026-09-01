"""通知通道 seam — Definition(DESIGN-story-butler §3.1)。

仓库 capability-seam 约定(AGENTS.md「新能力先立 seam」):本模块只声明中立接口,
实现(Provider)可多个并存;消费方只 import 这里,绝不 import 具体实现。
范例同构:``knowledge/adapters/base.py``(BaseAdapter)、``sourcing/sources/base.py``
(StorySource)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# 消息档位(DESIGN-story-butler §3.1 分档语义):
# - interrupt:立即走微信 + 桌面弹窗(每条对应一个待决策事项,打断要省着用)
# - batch:只落桌面弹窗/攒着,进晨报
# - digest:只进晨报(P2 定时器,v1 落桌面)
TIER_INTERRUPT = "interrupt"
TIER_BATCH = "batch"
TIER_DIGEST = "digest"


class NotificationChannel(ABC):
    """通知通道抽象(Definition)。

    契约:
    - ``send`` 失败返回 False,**绝不抛**(通知是 best-effort,投递线程按 False
      走退避重试)。
    - ``available`` 依赖缺失/未配置时返回 False,投递线程把该通道标 skipped
      (不重试一个永远发不出去的通道)。
    """

    #: 通道名(路由器 ChannelAction.channel 与 build_channels 注册表的键)
    name: str = ""

    @abstractmethod
    def send(self, title: str, message: str, tier: str = TIER_BATCH) -> bool:
        """发送一条通知。成功 True / 失败 False,绝不抛。"""

    @abstractmethod
    def available(self) -> bool:
        """通道当前是否可用(软依赖缺失/配置为空 → False)。"""
