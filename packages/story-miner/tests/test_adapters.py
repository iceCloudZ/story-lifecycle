"""Adapter 单测：合成 sanitized fixture，绝不内联真实对话(PII 红线)。"""
import sqlite3
from miner import common, store
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


CLAUDE_USAGE = (
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ok"}],'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":200,'
    '"cache_creation_input_tokens":0,"output_tokens":50}},'
    '"timestamp":"2026-06-27T10:00:01.000Z","cwd":"D:/github"}\n'
)


def test_claude_usage_to_tokens(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(CLAUDE_USAGE, encoding="utf-8")
    meta, evs, tokens = ClaudeAdapter().parse(str(f), "claude:s")
    assert len(tokens) == 1
    t = tokens[0]
    assert t["input_tokens"] == 100 and t["cache_read_tokens"] == 200
    assert t["output_tokens"] == 50 and t["src"] == "claude"


def test_token_usage_table_created(tmp_path):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)")]
    conn.close()
    for c in ("sid", "src", "ts", "model", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens"):
        assert c in cols
