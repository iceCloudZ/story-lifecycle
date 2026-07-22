"""Awaiting detector —— PTY 轨(codex/kimi)识别"AI 在等人"。

**轨划分(§2.1/§2.3,务必先读懂)**:
- **codex/kimi 轨**:走 PTY → 用本模块的正则 pattern 识别提问(agent-yes 三层抽象)。
- **Claude 轨**:**不走 PTY**,走 ``claude -p --output-format stream-json`` 的结构化事件
  (``permission_request`` / ``idle_prompt`` / ``elicitation_dialog``),见 ``claude_stream.py``(0b-1)。
  Claude 的"在等人"是 JSON 事件,比 PTY 正则更稳——**不要给 Claude 加 PTY pattern**。

两轨产出**统一的 ``(question, options)``,喂同一个 ``decide_response`` 决策大脑**。

Pattern 抽象借 ``snomiao/agent-yes`` 的 per-CLI ``{readyPatterns, enterPatterns, fatalPatterns}``:
本模块先用 ``enterPatterns`` 命中"待选择/确认";``ready``/``fatal`` 留后续(``fatal`` 给层3 recovery)。
options 优先正则提取字母/数字编号;``(Y/n)`` 类反映大小写;命中但无显式选项 → 默认二元 [是, 否]。

注:真实 codex/kimi 提问 pattern 在 0c-3 研究阶段实跑抓取后精调;当前是常见 pattern 的合理初始集。
"""

from __future__ import annotations

import re
from typing import Callable

# agent-yes 三层 pattern 的 enter 子集:命中表示 agent 在等人(待选择/确认/答复)。
#
# **只收强信号** —— 必须是明确的选项菜单/确认提示。
# 不要加"行尾问号 \?[ \t]*$"这类弱信号:kimi/codex 正常思考输出里大量以"?"
# 结尾的反问句/中文疑问句会被误判成"在等人",supervisor 就会塞一个 [是,否]
# 选择题并烧一次 LLM token。历史回归见 test_trailing_question_mark_is_not_awaiting。
_DEFAULT_ENTER_PATTERNS: list[str] = [
    r"请选择",
    r"选择\s*[：:]\s",  # "选择: " / "选择："
    r"\(\s*[Yy]\s*/\s*[Nn]\s*\)",  # (Y/n) / (y/N) — 二元确认
    r"\byes\s*/\s*no\b",
    r"Select\s+an\s+option",
    r"Choose\s+(?:an?\s+)?\w",
]

# per-adapter 覆盖(未来从 knowledge/adapters/{codex,shell}.yml 读;先硬编码)
# 注:"claude" 故意不在表里 —— 走 stream-json 轨,不经 PTY pattern(见模块 docstring)。
_ENTER_PATTERNS: dict[str, list[str]] = {
    "codex": _DEFAULT_ENTER_PATTERNS,
    "kimi": _DEFAULT_ENTER_PATTERNS,
    "shell": _DEFAULT_ENTER_PATTERNS,
}

# 选项编号:A) / B. / 1、 / 2） 等,后跟实文本(排除 "(Y/n)" 里的 Y/n)
_OPTION_RE = re.compile(r"\b([A-Za-z0-9])\s*[).．、）]\s*\S")

# (Y/n) 类二元确认 —— 大小写照原样
_YN_RE = re.compile(r"\(\s*([Yy])\s*/\s*([Nn])\s*\)")

# (Y/n) 类二元确认命中后无显式编号选项时,从提示里照大小写取 options。
# **不再**对无选项的提问兜底 [是, 否] —— 那会把 agent 的正常疑问句误判成
# 二元选择,supervisor 就烧一次 LLM 并往 PTY 塞噪声(历史回归见
# test_no_fallback_binary_for_bare_question)。

# ANSI 转义序列(真实 PTY/winpty 输出里大量出现:CSI 颜色/光标、OSC 标题、字符集)
# E2E 实跑发现不剥离会污染 question 字段 + 干扰 pattern 匹配。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"  # CSI: \x1b[...m / [?25h / [H 等
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: \x1b]0;title\x07
    r"|\x1b[()][AB012]"  # 字符集切换 \x1b(B
)


def make_awaiting_fn(
    adapter: str,
) -> Callable[[str], tuple[str, list[str]] | None]:
    """构建 PTY 轨的 awaiting 识别器。

    Args:
        adapter: CLI 名(codex/kimi/shell/...)。未知 adapter 用默认 pattern 集。
            **不要传 "claude"** —— Claude 走 stream-json 轨(``claude_stream.py``,0b-1)。

    Returns:
        ``detect(buffer) -> (question, options) | None``。

        - 命中提问 → ``(命中所在行, options)``;options 优先正则提取字母/数字编号,
          ``(Y/n)`` 类反映大小写,否则默认二元 ``[是, 否]``。
        - 未命中 → ``None``(supervisor 短路,不调 LLM,省 token)。
    """
    patterns = _ENTER_PATTERNS.get(adapter, _DEFAULT_ENTER_PATTERNS)
    compiled = [re.compile(p) for p in patterns]

    def detect(buffer: str) -> tuple[str, list[str]] | None:
        buffer = _ANSI_RE.sub("", buffer)  # 剥离 winpty/终端 ANSI 转义
        match = None
        for rx in compiled:
            match = rx.search(buffer)
            if match:
                break
        if not match:
            return None
        line = _line_around(buffer, match.start())
        tail = buffer[match.start() :]
        options = _extract_options(tail)
        if not options:
            options = _default_options_for(tail)
        if not options:
            return None
        return (line, options)

    return detect


def _line_around(buffer: str, pos: int) -> str:
    """pos 所在行(去首尾空白),作为 question 文本。"""
    start = buffer.rfind("\n", 0, pos) + 1
    end = buffer.find("\n", pos)
    if end == -1:
        end = len(buffer)
    return buffer[start:end].strip()


def _extract_options(tail: str) -> list[str]:
    """从命中点之后提取选项编号(字母/数字),保序去重。"""
    seen: list[str] = []
    for m in _OPTION_RE.finditer(tail):
        letter = m.group(1)
        if letter not in seen:
            seen.append(letter)
    return seen


def _default_options_for(tail: str) -> list[str]:
    """无显式编号选项时:仅 (Y/n) 类二元确认照大小写提取;其余返回空(不兜底)。

    返回空 → detect 返回 None → supervisor 不调 LLM、不写 PTY。
    这避免了对 agent 正常疑问句(行尾问号)的误应答。
    """
    m = _YN_RE.search(tail)
    if m:
        return [m.group(1), m.group(2)]
    return []
