"""Adapter 单测：合成 sanitized fixture，绝不内联真实对话(PII 红线)。"""
from miner.adapters.claude import ClaudeAdapter

CLAUDE_FIXTURE = (
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"hi"}]},'
    '"timestamp":"2026-06-27T10:00:00.000Z","cwd":"D:/github"}\n'
)


def test_parse_returns_three_tuple(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(CLAUDE_FIXTURE, encoding="utf-8")
    meta, evs, tokens = ClaudeAdapter().parse(str(f), "claude:s")
    assert isinstance(meta, dict)
    assert isinstance(evs, list)
    assert isinstance(tokens, list)  # 暂为空
