"""Task Actions — 预制动作库 + prompt 组装(DESIGN-task-actions-and-grill-me.md)。

把"stage 该干什么"从靠 stage 名隐含改成 LLM 显式选动作清单。
编排器 LLM 从这个预制库里为每个 stage 选该做哪些活,拼成任务清单。

**设计原则**:
- 动作库是编排层预制的,LLM **只选不编**——不能自己造新动作
- 每个动作有 order 字段,Python 按 order 排序(LLM 只管选,Python 负责排)
- 每个动作有 mode 字段(grill-me 下一步用:autonomous/interactive)
- 每个动作有 expected_output_key(联动 done.json 校验,Q3)

理论依据:Meta-Prompting(arXiv:2312.06562)/ Mixture of Prompts(AAAI 2025)。
"""

from __future__ import annotations


# ---- 动作库 ----

TASK_ACTIONS: dict[str, dict] = {
    "write_design_doc": {
        "desc": "调研现有代码，产出设计方案（数据流/接口/表结构/状态机）",
        "instruction": (
            "先调研现有代码结构和链路，产出设计方案。覆盖：数据流、接口契约、"
            "数据模型（表/字段/索引）、核心逻辑、一致性并发、边界异常、安全。"
            "可参考 kb.py graph 查依赖关系。"
        ),
        "order": 10,
        "mode": "interactive",  # grill-me:设计阶段可追问拉扯
        "expected_output_key": "spec_path",
    },
    "write_code": {
        "desc": "按需求实现代码改动",
        "instruction": "按设计方案实现代码改动。改完确认语法/类型无误。",
        "order": 20,
        "mode": "autonomous",
        "expected_output_key": "files_changed",
    },
    "run_tests": {
        "desc": "运行测试确认改动正确",
        "instruction": (
            "运行测试（pytest/ruff check）确认改动正确。测试失败就修，直到通过。"
        ),
        "order": 30,
        "mode": "autonomous",
        "expected_output_key": None,
    },
    "accept_review": {
        "desc": "自验收：对照需求逐条确认完成度",
        "instruction": (
            "对照 PRD 需求逐条自验收。未完成的补上，确认所有需求点覆盖。"
            "仅在不确定性高、涉及重大业务逻辑变更时才提问；改动微小且明确可直接通过。"
        ),
        "order": 40,
        "mode": "interactive",
        "expected_output_key": None,
    },
    "write_test_report": {
        "desc": "产出测试报告",
        "instruction": "产出测试报告：测了什么、结果、覆盖率。",
        "order": 50,
        "mode": "autonomous",
        "expected_output_key": "test_report_path",
    },
    "write_delivery_doc": {
        "desc": "产出交付文档（变更摘要/影响面/回滚）",
        "instruction": "产出交付文档：变更摘要、影响面分析、回滚方案。",
        "order": 60,
        "mode": "autonomous",
        "expected_output_key": "delivery_path",
    },
}


# ---- 默认动作组合(fallback:LLM 不可用时按 stage 名给) ----

_DEFAULT_TASK_ACTIONS: dict[str, list[str]] = {
    "design": ["write_design_doc"],
    "build": ["write_code"],
    "verify": ["run_tests", "accept_review", "write_test_report"],
}

# 单 stage profile(single-pass 等)→ 全干
_DEFAULT_SINGLE_STAGE_ACTIONS: list[str] = [
    "write_design_doc",
    "write_code",
    "run_tests",
    "accept_review",
    "write_test_report",
]


def get_default_task_actions(stage: str, is_single_stage: bool = False) -> list[str]:
    """fallback:LLM 不可用时,按 stage 名给默认动作。

    单 stage profile → 全干(设计+编码+测试+验收+报告)。
    """
    if is_single_stage:
        return list(_DEFAULT_SINGLE_STAGE_ACTIONS)
    return list(_DEFAULT_TASK_ACTIONS.get(stage, ["write_code"]))


# ---- prompt 组装 ----


def _build_task_list(action_keys: list[str]) -> str:
    """把 LLM 选的动作 key 列表 → prompt 里的任务清单段。

    按 order 排序(R1:LLM 只管选,Python 负责排,保证逻辑顺序)。
    """
    # 过滤无效 key + 按 order 排序
    valid = []
    for key in action_keys:
        action = TASK_ACTIONS.get(key)
        if action:
            valid.append((key, action))
    valid.sort(key=lambda x: x[1]["order"])

    if not valid:
        return ""

    items = []
    for i, (key, action) in enumerate(valid, 1):
        items.append(f"{i}. {action['instruction']}")
    return "\n### 本阶段任务清单\n请按以下顺序完成：\n" + "\n".join(items) + "\n"


def _build_exec_constraint(action_keys: list[str]) -> str:
    """根据 task_actions 内容决定执行约束(替 _is_single_stage 硬编码)。

    选了 run_tests → 允许跑轻量测试。
    没选 → 只写代码,不需要跑测试。
    都禁重构建。
    """
    has_tests = "run_tests" in action_keys
    if has_tests:
        return (
            "\n### 执行约束\n"
            "可以跑轻量自检（pytest/ruff check/tsc --noEmit）确认改动正确，"
            "但**不要跑重构建**（mvn/npm install/yarn install）——"
            "它们在大型仓库上常阻塞超 10 分钟。\n"
        )
    return (
        "\n### 执行约束\n"
        "本阶段只写代码/文档，不需要跑测试。"
        "**不要运行**任何构建/测试命令（mvn/gradle/pytest 等）——"
        "它们在大型仓库上常阻塞超 10 分钟。\n"
    )


def get_expected_outputs(action_keys: list[str]) -> list[str]:
    """从 task_actions 推导该 stage 期望的 done.json 字段(Q3 联动)。

    选了 write_design_doc → 期望 spec_path。
    选了 write_test_report → 期望 test_report_path。
    用于 done 协议里列出期望字段。
    """
    outputs = []
    for key in action_keys:
        action = TASK_ACTIONS.get(key, {})
        out_key = action.get("expected_output_key")
        if out_key and out_key not in outputs:
            outputs.append(out_key)
    return outputs


def build_done_protocol(stage: str, done_file: str, action_keys: list[str]) -> str:
    """构建完成协议段(done.json 格式)。

    根据选的 task_actions 动态列出期望字段(Q3:一鱼两吃)。
    """
    expected = get_expected_outputs(action_keys)
    # 基础字段(所有 done 都有)
    fields = {
        "stage": stage,
        "status": "done",
        "summary": "完成摘要",
        "files_changed": [],
    }
    # 动态追加期望字段
    for out_key in expected:
        if out_key not in fields:
            fields[out_key] = f"<{out_key}>"

    fields_str = ", ".join(f'"{k}": {v!r}' for k, v in fields.items())
    return (
        f"\n### 完成协议\n"
        f"完成后必须写入文件 `{done_file}`，内容为 JSON:\n"
        f"{{{fields_str}}}\n\n"
        f"注意：JSON 必须是纯 JSON，不要包裹在 markdown 代码块中。"
    )


# ---- system prompt 辅助:给 LLM 看的可选动作列表 ----


def get_action_catalog_for_prompt() -> str:
    """给 system prompt 用的动作目录(帮 LLM 选)。

    含推荐模式(Q2:给常识不给硬规则)。
    """
    lines = ["## 可选任务动作（为每个 stage 选该做哪些）"]
    for key, action in sorted(TASK_ACTIONS.items(), key=lambda x: x[1]["order"]):
        lines.append(f"- {key}: {action['desc']}")
    lines.append("")
    lines.append("**推荐模式（常识，非硬规则——根据需求描述灵活调整）：**")
    lines.append("- 单阶段全干：选全部动作")
    lines.append("- 多阶段 design：侧重 write_design_doc")
    lines.append("- 多阶段 build：侧重 write_code")
    lines.append("- 多阶段 verify：侧重 run_tests + accept_review + write_test_report")
    lines.append("- 纯前端需求可加 write_delivery_doc（影响面/回滚）")
    lines.append("- Hotfix/小改动可跳过 write_design_doc/write_test_report")
    return "\n".join(lines)
