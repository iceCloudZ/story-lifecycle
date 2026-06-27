"""Adapter 单测：合成 sanitized fixture，绝不内联真实对话(PII 红线)。"""
from miner import common
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


def test_full_ts_iso():
    assert common.full_ts({"timestamp": "2026-06-27T10:00:00.5Z"}).startswith("2026-06-27T10:00:00")


def test_full_ts_ms():
    # 1781688000000 ms -> 2026-... ISO,非空且含 'T'
    s = common.full_ts({"time": 1781688000000})
    assert s and "T" in s and len(s) >= 19


def test_full_ts_fallback():
    assert common.full_ts({}, "FB") == "FB"
