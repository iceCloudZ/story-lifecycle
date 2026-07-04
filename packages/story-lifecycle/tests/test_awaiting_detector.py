"""Tests for awaiting_detector —— 识别 codex/kimi 在 PTY 里"在等人"。

借 ``snomiao/agent-yes`` 三层 pattern 抽象(per-CLI ``{ready, enter, fatal}``),
``make_awaiting_fn(adapter)`` 返回 ``detect(buffer) -> (question, options) | None``。
先硬编码常见提问 pattern(选择/确认/Y-n/行尾?),options 用正则提取字母/数字;
真实 PTY pattern 的精调在 0c-3 研究阶段(实跑 codex/kimi 抓输出)。
"""

from story_lifecycle.orchestrator.engine.awaiting_detector import make_awaiting_fn


class TestMakeAwaitingFn:
    def test_detects_chinese_select_with_lettered_options(self):
        """命中"请选择"+ 字母选项 → (question, ["A","B"])。"""
        detect = make_awaiting_fn("codex")
        result = detect("请选择方案: A) 重试 B) 跳过")
        assert result is not None
        question, options = result
        assert "请选择方案" in question
        assert options == ["A", "B"]

    def test_returns_none_on_normal_output(self):
        """普通工作输出(无提问信号)→ None(不误触发 supervisor)。"""
        detect = make_awaiting_fn("codex")
        assert detect("正在编辑 src/app.py\n文件已保存\n下一步运行测试") is None

    def test_detects_yes_no_prompt_options(self):
        """(Y/n) 类提示 → options 反映大小写(["Y","n"] 或 ["y","N"])。"""
        detect = make_awaiting_fn("kimi")
        result = detect("确认继续执行吗? (Y/n)")
        assert result is not None
        _question, options = result
        assert options == ["Y", "n"]

    def test_detects_yes_no_prompt_lowercase(self):
        detect = make_awaiting_fn("kimi")
        result = detect("是否应用此更改? (y/N)")
        assert result is not None
        assert result[1] == ["y", "N"]

    def test_detects_numeric_options(self):
        """数字编号选项 1) 2) 3) → ["1","2","3"]。"""
        detect = make_awaiting_fn("codex")
        result = detect("请选择:\n1) 重新生成\n2) 修改后继续\n3) 放弃")
        assert result is not None
        assert result[1] == ["1", "2", "3"]

    def test_unknown_adapter_falls_back_to_default_patterns(self):
        """未知 adapter 仍能用默认 pattern 集(不崩)。"""
        detect = make_awaiting_fn("some-unknown-cli")
        result = detect("请选择: A) foo B) bar")
        assert result is not None
        assert result[1] == ["A", "B"]

    def test_trailing_question_mark_alone_is_awaiting(self):
        """行尾问号(无显式选项)→ 视为 binary 询问,给默认 [是, 否] options。"""
        detect = make_awaiting_fn("codex")
        result = detect("要继续吗?")
        assert result is not None
        _question, options = result
        assert len(options) >= 2  # 至少 是/否 二元

    def test_strips_ansi_escape_sequences_from_question(self):
        """真实 PTY(winpty)输出含 ANSI 转义(标题/光标/颜色),detector 要先剥离。

        E2E 实跑发现:question 字段带了 ``\x1b]0;title\x07\x1b[?25h`` 前缀,
        污染日志 + 可能干扰 LLM。剥离后 question 应干净。
        """
        detect = make_awaiting_fn("codex")
        # 真实 winpty 输出样例:设标题 + 显隐光标 + 实际问题
        buf = "\x1b]0;C:\\python.exe\x07\x1b[?25h请选择: A) foo B) bar\r\n"
        result = detect(buf)
        assert result is not None
        question, options = result
        assert "\x1b" not in question  # 无 ANSI 残留
        assert "请选择" in question
        assert options == ["A", "B"]
