"""Tests for the design-stage clarification detection layer (runbook 块3).

design 阶段「claude 逐问 + 人答」的提问检测。claude -p 无 AskUserQuestion 工具
(实测:claude 明言 "There is no AskUserQuestion tool in my environment"),故主路径
是**侧文件协议**——claude 遇关键歧义写 ``clarify_request.json`` 后退出;stream 上的
``<<CLARIFY>>`` marker 与 AskUserQuestion tool_use 作防御(供未来 PTY/工具可用时)。

检测函数纯(文件/行 → dict|None),DB/SSE/回注由编排层接(详见 docs/design-hitl-runbook.md)。
"""

import json

from story_lifecycle.orchestrator.engine.clarify import (
    CLARIFY_MARKER,
    CLARIFY_REQUEST_FILENAME,
    append_clarify_history,
    clarify_request_rel,
    clear_clarify_request,
    consume_clarify_answer,
    extract_clarification_from_stream,
    read_clarify_history,
    read_clarify_request,
)


class TestReadClarifyRequest:
    def test_reads_valid_side_file(self, tmp_path):
        """侧文件含 id/question/header/options → 返回规整 dict。"""
        (tmp_path / CLARIFY_REQUEST_FILENAME).write_text(
            json.dumps(
                {
                    "id": "q1",
                    "question": "配置存 hc_user 还是 hc_config?",
                    "header": "存储位置",
                    "options": ["hc_user", "hc_config"],
                    "context": "联系人姓名需校验",
                }
            ),
            encoding="utf-8",
        )

        req = read_clarify_request(tmp_path / CLARIFY_REQUEST_FILENAME)

        assert req is not None
        assert req["id"] == "q1"
        assert req["question"] == "配置存 hc_user 还是 hc_config?"
        assert req["header"] == "存储位置"
        assert req["options"] == ["hc_user", "hc_config"]
        assert req["context"] == "联系人姓名需校验"

    def test_missing_file_returns_none(self, tmp_path):
        """文件不存在 → None(poll loop 正常分支,不报错)。"""
        assert read_clarify_request(tmp_path / CLARIFY_REQUEST_FILENAME) is None

    def test_corrupt_json_returns_none(self, tmp_path):
        """损坏 JSON → None(failsafe,绝不阻塞 poll loop)。"""
        (tmp_path / CLARIFY_REQUEST_FILENAME).write_text("{not json", encoding="utf-8")
        assert read_clarify_request(tmp_path / CLARIFY_REQUEST_FILENAME) is None

    def test_empty_options_returns_none(self, tmp_path):
        """options 空/缺 → None(不是有效提问;claude 应自决而非空问)。"""
        (tmp_path / CLARIFY_REQUEST_FILENAME).write_text(
            json.dumps({"id": "q", "question": "q?", "options": []}),
            encoding="utf-8",
        )
        assert read_clarify_request(tmp_path / CLARIFY_REQUEST_FILENAME) is None

    def test_missing_question_returns_none(self, tmp_path):
        """缺 question → None(无效)。"""
        (tmp_path / CLARIFY_REQUEST_FILENAME).write_text(
            json.dumps({"id": "q", "options": ["a"]}), encoding="utf-8"
        )
        assert read_clarify_request(tmp_path / CLARIFY_REQUEST_FILENAME) is None


class TestClearClarifyRequest:
    def test_deletes_existing_file(self, tmp_path):
        p = tmp_path / CLARIFY_REQUEST_FILENAME
        p.write_text("{}", encoding="utf-8")
        assert clear_clarify_request(p) is True
        assert not p.exists()

    def test_missing_file_returns_false(self, tmp_path):
        assert clear_clarify_request(tmp_path / CLARIFY_REQUEST_FILENAME) is False


class TestClarifyRequestRel:
    def test_rel_path_takes_done_file_dir_forward_slash(self):
        """侧文件相对路径取自 done file 同目录(正斜杠,与 poll loop Path() 兼容)。

        prompt 注入与 poll loop 查的是同一文件,路径必须一致 —— 集中到此函数。
        """
        rel = clarify_request_rel(".story/done/S-1/design.json")
        assert rel == ".story/done/S-1/clarify_request.json"

    def test_rel_path_handles_already_forward_slash(self):
        rel = clarify_request_rel(".story/done/S-1/design.json")
        assert "\\" not in rel  # 跨 OS 统一正斜杠(prompt/claude 句柄)


class TestClarifyHistory:
    """累计 Q&A 历史(clarify_history.json)——回注后带它重启 claude,实现
    「前答影响后问」(动态澄清)。runbook 块5 回注侧的数据载体。"""

    def test_read_missing_returns_empty(self, tmp_path):
        assert read_clarify_history(tmp_path / "clarify_history.json") == []

    def test_append_creates_and_returns_list(self, tmp_path):
        p = tmp_path / "clarify_history.json"
        out = append_clarify_history(p, question="存哪?", answer="hc_user")
        assert out == [{"question": "存哪?", "answer": "hc_user"}]
        assert read_clarify_history(p) == [{"question": "存哪?", "answer": "hc_user"}]

    def test_append_accumulates_across_rounds(self, tmp_path):
        p = tmp_path / "clarify_history.json"
        append_clarify_history(p, "q1", "a1")
        append_clarify_history(p, "q2", "a2")
        assert read_clarify_history(p) == [
            {"question": "q1", "answer": "a1"},
            {"question": "q2", "answer": "a2"},
        ]

    def test_read_corrupt_returns_empty(self, tmp_path):
        p = tmp_path / "clarify_history.json"
        p.write_text("not json", encoding="utf-8")
        assert read_clarify_history(p) == []


class TestConsumeClarifyAnswer:
    """POST /clarify/answer 的核心:读待答 request → 累计 history → 清 request。
    纯文件操作(路径 caller 算);DB/重驱动(start_story_async)归 API 层。"""

    def test_consumes_pending_appends_history_clears_request(self, tmp_path):
        req_path = tmp_path / CLARIFY_REQUEST_FILENAME
        hist_path = tmp_path / "clarify_history.json"
        req_path.write_text(
            json.dumps(
                {
                    "id": "q1",
                    "question": "存哪?",
                    "header": "存储",
                    "options": ["hc_user", "hc_config"],
                }
            ),
            encoding="utf-8",
        )

        out = consume_clarify_answer(req_path, hist_path, "hc_user")

        assert out == {"question": "存哪?", "answer": "hc_user", "id": "q1"}
        assert not req_path.exists()  # request 清掉(防 resume 重复触发)
        assert read_clarify_history(hist_path) == [
            {"question": "存哪?", "answer": "hc_user"}
        ]

    def test_no_pending_returns_none(self, tmp_path):
        req_path = tmp_path / CLARIFY_REQUEST_FILENAME
        hist_path = tmp_path / "clarify_history.json"
        # 无 request 文件
        assert consume_clarify_answer(req_path, hist_path, "x") is None
        assert read_clarify_history(hist_path) == []  # 未累计

    def test_consume_accumulates_across_rounds(self, tmp_path):
        req_path = tmp_path / CLARIFY_REQUEST_FILENAME
        hist_path = tmp_path / "clarify_history.json"
        # 第一轮
        req_path.write_text(
            json.dumps({"id": "q1", "question": "Q1", "options": ["a", "b"]}),
            encoding="utf-8",
        )
        consume_clarify_answer(req_path, hist_path, "A1")
        # 第二轮(claude 重启后又写了新 request)
        req_path.write_text(
            json.dumps({"id": "q2", "question": "Q2", "options": ["c", "d"]}),
            encoding="utf-8",
        )
        consume_clarify_answer(req_path, hist_path, "A2")

        assert read_clarify_history(hist_path) == [
            {"question": "Q1", "answer": "A1"},
            {"question": "Q2", "answer": "A2"},
        ]


class TestExtractClarificationFromStream:
    def test_marker_in_assistant_text_yields_request(self):
        """assistant text 内 `<<CLARIFY>> {json}` → 提问 dict(主防御路径)。"""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"分析完,有个岔路 {CLARIFY_MARKER} "
                            + json.dumps(
                                {
                                    "id": "m1",
                                    "question": "存哪?",
                                    "header": "存储",
                                    "options": ["hc_user", "hc_config"],
                                }
                            ),
                        }
                    ]
                },
            }
        )

        req = extract_clarification_from_stream(line)

        assert req is not None
        assert req["question"] == "存哪?"
        assert req["options"] == ["hc_user", "hc_config"]
        assert req["header"] == "存储"

    def test_askuserquestion_tooluse_yields_request(self):
        """AskUserQuestion tool_use(防御;claude -p 暂无此工具,但 PTY/未来可用)。"""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "用 A 还是 B?",
                                        "header": "方案",
                                        "options": [
                                            {"label": "A", "description": "a"},
                                            {"label": "B", "description": "b"},
                                        ],
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        )

        req = extract_clarification_from_stream(line)

        assert req is not None
        assert req["question"] == "用 A 还是 B?"
        assert req["options"] == ["A", "B"]  # label 列表
        assert req["header"] == "方案"

    def test_plain_assistant_text_returns_none(self):
        """普通文本输出(无 marker)→ None(不误判)。"""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "正在分析..."}]},
            }
        )
        assert extract_clarification_from_stream(line) is None

    def test_normal_tool_use_returns_none(self):
        """普通工具调用(Read/Write/Bash)→ None。"""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t", "name": "Read", "input": {}}
                    ]
                },
            }
        )
        assert extract_clarification_from_stream(line) is None

    def test_non_json_line_returns_none(self):
        assert extract_clarification_from_stream("not json at all") is None
