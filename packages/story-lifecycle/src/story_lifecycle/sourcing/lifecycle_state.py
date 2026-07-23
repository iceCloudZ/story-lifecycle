"""Story 业务状态 enum(TABS-LIFECYCLE-STATE)。

lifecycle_state 是 story 的固有业务状态(待启动/开发/测试/上线/结项),独立于
引擎 status。Grok-build §2.2:状态值 enum 化,消灭散落的硬编码 ``"开发"``/
``"待启动"`` 字面量(planner.py/api.py 多处 fallback)。

YAML(source_profiles)里仍是中文字面量(向后兼容),代码侧用本 enum。
"""

from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    """Story 业务状态 — 四个主 tab 的判据(互斥)。"""

    PENDING = "待启动"  # start 后、确认规划前(DB DEFAULT)
    DEV = "开发"  # /plan/confirm 后
    TEST = "测试"  # /lifecycle/advance 后
    ONLINE = "上线"  # 测试通过
    CLOSED = "结项"  # 上线验证完成 / 归档 / TAPD closed 映射


# tab 判据用的状态集合(对应四个主 tab 的过滤条件)。
# 待启动 tab
PENDING_VALUE = LifecycleState.PENDING.value
# 开发中 tab
DEV_VALUE = LifecycleState.DEV.value
# 测试·上线 tab
TEST_ONLINE_VALUES = frozenset({LifecycleState.TEST.value, LifecycleState.ONLINE.value})
# 已结项 tab
CLOSED_VALUE = LifecycleState.CLOSED.value

# 所有非结项状态(未完结 = 还在流程里)。list_active_stories 用。
NON_CLOSED_VALUES = frozenset(
    {
        LifecycleState.PENDING.value,
        LifecycleState.DEV.value,
        LifecycleState.TEST.value,
        LifecycleState.ONLINE.value,
    }
)


def resolve_lifecycle_state(ctx_lifecycle: str | None, db_lifecycle: str | None) -> str:
    """三段 fallback:ctx > DB > 待启动。

    替掉 planner.py:862 / api.py:1043 / api.py:3441 散落的三处
    ``ctx.get(...) or story.get(...) or "待启动"``。
    """
    return ctx_lifecycle or db_lifecycle or LifecycleState.PENDING.value
