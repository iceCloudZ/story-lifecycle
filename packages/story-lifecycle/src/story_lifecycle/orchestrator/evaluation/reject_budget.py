"""reject 上限防护(STEP 2.1,DESIGN §4.9 / 评审 A2)。

防 false reject 打回循环烧 token:同 stage reject 次数上限 + 每次 reject 必须给与
上次不同的具体理由 + 超限强制 escalate_human。

**为什么需要**:同一成果物两次无状态唤起可能给不同 verdict(评审 A1 self-grading
同族相关性)。reject→重跑→再 reject 会无限烧 token。上限 + 理由去重 + 超限 escalate
是显式防护(设计 §7.2)。

纯函数(读 DB,零 LLM,零副作用)。boundary_judge 的 reject 决策先过这里。
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("story-lifecycle.reject_budget")

# reject 次数上限(可配)。同 stage reject 数 >= 此值 → 强制 escalate。
# 默认 3:给 code agent 3 次重试机会,第 4 次 reject 转人(防无限打回)。
_DEFAULT_REJECT_LIMIT = 3
REJECT_LIMIT = int(os.environ.get("STORY_REJECT_LIMIT", _DEFAULT_REJECT_LIMIT))

# 理由归一化:去空白/标点/大小写后比较,防 judge 换个措辞绕过去重(评审 A2)。
_NORMALIZE_KEEP = re.compile(r"[\w\u4e00-\u9fff]")


def _normalize_reason(reason: str) -> str:
    """归一化 reject 理由(去空白/标点/转小写),用于去重比较。

    保留字母数字 + 中文,去掉所有标点/空白/大小写差异。这样 judge 把
    "缺测试" 写成 "缺测试。" 或 "缺测试!" 仍判同一理由(防措辞绕过)。
    """
    if not reason:
        return ""
    return "".join(_NORMALIZE_KEEP.findall(reason)).lower()


def check_reject_budget(
    story_key: str,
    stage: str,
    new_reason: str,
    *,
    limit: int | None = None,
    trigger: str = "boundary_judge",
    db_module=None,
) -> dict:
    """检查本次 reject 是否被允许(防打回循环,§4.9 / 评审 A2)。

    Args:
        story_key / stage: 当前 story+stage。
        new_reason: 本次 reject 的新理由。
        limit: reject 上限(默认 REJECT_LIMIT,可 env 配)。
        trigger: 决策触发源——默认 "boundary_judge"；外部测试 FAIL 走
            "external_verify"(设计 10 改动 1.3,修订点 R2:外部失败同样计
            reject budget,防环境挂/journey 坏时无限 retry 死循环)。
        db_module: 注入 db(测试用);None 则延迟 import。

    Returns:
        {"allow": bool, "force": str | None, "warn": str | None, "count": int}
        - allow=True:可以 reject。
        - allow=False + force="escalate":reject 超上限或理由重复,强制 escalate_human。
        - count:该 stage 历史 reject 次数(含本次前)。
    """
    if db_module is None:
        from ...infra.db import models as db_module

    budget = limit if limit is not None else REJECT_LIMIT

    # 查该 stage 历史 reject 数(boundary_judge / external_verify 触发)。
    try:
        count = db_module.count_decisions(
            story_key, stage, decision="reject", trigger=trigger
        )
    except Exception as exc:  # noqa: BLE001 — 查询失败安全放行(不阻塞主流程)
        log.warning(
            "[%s/%s] count_decisions failed, allowing reject: %s",
            story_key,
            stage,
            exc,
        )
        return {"allow": True, "force": None, "warn": None, "count": 0}

    # 规则 1:超上限 → 强制 escalate(防无限打回)。
    if count >= budget:
        log.warning(
            "[%s/%s] reject 上限已达 %d/%d → 强制 escalate(防打回循环)",
            story_key,
            stage,
            count,
            budget,
        )
        return {
            "allow": False,
            "force": "escalate",
            "warn": f"reject 超上限({count}/{budget})",
            "count": count,
        }

    # 规则 2:理由与上次 reject 重复 → judge 在抖,强制 escalate(评审 A2)。
    try:
        decisions = db_module.get_decisions(story_key, stage, trigger=trigger, limit=10)
    except Exception:  # noqa: BLE001
        decisions = []
    prev_rejects = [d for d in decisions if d.get("decision") == "reject"]
    if prev_rejects:
        last_reason = prev_rejects[0].get("reason", "")
        if (
            _normalize_reason(last_reason) == _normalize_reason(new_reason)
            and new_reason
        ):
            log.warning(
                "[%s/%s] reject 理由与上次重复(%r)→ judge 抖,强制 escalate",
                story_key,
                stage,
                new_reason,
            )
            return {
                "allow": False,
                "force": "escalate",
                "warn": f"reject 理由重复(上次:{last_reason[:60]})",
                "count": count,
            }

    return {"allow": True, "force": None, "warn": None, "count": count}
