"""Adapter 单测：合成 sanitized fixture，绝不内联真实对话(PII 红线)。"""
import sqlite3
from miner import common, store
from miner.adapters.claude import ClaudeAdapter
from miner.adapters.codex import CodexAdapter
from miner.adapters.kimi import KimiAdapter

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


CODEX_USAGE = (
    '{"type":"event_msg","timestamp":"2026-05-23T02:01:55.865Z",'
    '"payload":{"type":"token_count","cwd":"D:/github","info":{'
    '"total_token_usage":{"input_tokens":12191,"cached_input_tokens":9600,'
    '"output_tokens":343,"reasoning_output_tokens":94}}}}\n'
)


def test_codex_usage_to_tokens(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(CODEX_USAGE, encoding="utf-8")
    meta, evs, tokens = CodexAdapter().parse(str(f), "codex:r")
    assert len(tokens) == 1
    t = tokens[0]
    assert t["input_tokens"] == 12191 and t["cache_read_tokens"] == 9600
    assert t["reasoning_tokens"] == 94


KIMI_USAGE = (
    '{"type":"usage.record","time":1781688000000,"model":"kimi-for-coding",'
    '"usage":{"inputOther":3825,"output":185,"inputCacheRead":14848,'
    '"inputCacheCreation":0}}\n'
)


def test_kimi_usage_to_tokens_not_think(tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(KIMI_USAGE, encoding="utf-8")
    meta, evs, tokens = KimiAdapter().parse(str(f), "kimi:s:main")
    assert len(tokens) == 1
    assert tokens[0]["output_tokens"] == 185
    assert tokens[0]["cache_read_tokens"] == 14848
    # 不再产生 think 事件
    assert not any(e.get("kind") == "think" for e in evs)


KIMI_ERR = (
    '{"type":"context.append_loop_event","time":1781688001000,"event":'
    '{"tool_name":"Bash","result":{"output":"boom","isError":true}}}\n'
)


def test_kimi_iserror_to_result(tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(KIMI_ERR, encoding="utf-8")
    meta, evs, tokens = KimiAdapter().parse(str(f), "kimi:s:main")
    results = [e for e in evs if e.get("kind") == "result"]
    assert results and results[0]["ok"] == 0


def test_story_token_aggregation(tmp_path):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    conn = sqlite3.connect(db)
    # 生产里 link.py 用 ALTER 给 sessions 加 story_id 列；测试模拟该 post-link schema
    conn.execute("ALTER TABLE sessions ADD COLUMN story_id TEXT")
    conn.execute("INSERT INTO sessions(sid,story_id) VALUES('claude:a','S1'),('kimi:b',NULL)")
    conn.execute("INSERT INTO sessions(sid,story_id) VALUES('claude:c','S1')")
    conn.execute("INSERT INTO token_usage(sid,src,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens) "
                 "VALUES('claude:a','claude',100,50,200,0),('kimi:b','kimi',40,10,0,0),('claude:c','claude',60,30,100,0)")
    conn.commit(); conn.close()
    import importlib.util, os
    spec = importlib.util.spec_from_file_location("st",
        os.path.join("packages", "story-miner", "scripts", "story_token.py"))
    st = importlib.util.module_from_spec(spec); spec.loader.exec_module(st)
    per_story, unlinked = st.aggregate(db)
    # S1 = a+c: input 160, output 80, cache_read 300
    assert per_story["S1"]["input_tokens"] == 160 and per_story["S1"]["output_tokens"] == 80
    assert per_story["S1"]["cache_read_tokens"] == 300
    assert 0 < per_story["S1"]["cache_hit"] < 1   # 效率列存在
    # 未关联 = b
    assert unlinked["input_tokens"] == 40


def test_token_usage_table_created(tmp_path):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)")]
    conn.close()
    for c in ("sid", "src", "ts", "model", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens"):
        assert c in cols
