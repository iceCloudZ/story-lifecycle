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


CLAUDE_USAGE_DUP = (
    # 同一 assistant turn 写成多行(thinking/text/tool_use),共享 message.id,重复带同一 usage
    '{"type":"assistant","message":{"role":"assistant","id":"msg_T1",'
    '"content":[{"type":"thinking","thinking":"x"}],'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":200,'
    '"cache_creation_input_tokens":0,"output_tokens":50}},'
    '"timestamp":"2026-06-27T10:00:01.000Z","cwd":"D:/github"}\n'
    '{"type":"assistant","message":{"role":"assistant","id":"msg_T1",'
    '"content":[{"type":"text","text":"ok"}],'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":200,'
    '"cache_creation_input_tokens":0,"output_tokens":50}},'
    '"timestamp":"2026-06-27T10:00:02.000Z","cwd":"D:/github"}\n'
    '{"type":"assistant","message":{"role":"assistant","id":"msg_T1",'
    '"content":[{"type":"tool_use","name":"Bash","input":{}}],'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":200,'
    '"cache_creation_input_tokens":0,"output_tokens":50}},'
    '"timestamp":"2026-06-27T10:00:03.000Z","cwd":"D:/github"}\n'
)


def test_claude_usage_dedup_per_turn(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(CLAUDE_USAGE_DUP, encoding="utf-8")
    meta, evs, tokens = ClaudeAdapter().parse(str(f), "claude:s")
    # 同一 turn(message.id 相同)的 3 行只采一次 usage(原来采 3 次,虚高 ~3x)
    assert len(tokens) == 1
    assert tokens[0]["input_tokens"] == 100


CODEX_USAGE = (
    '{"type":"event_msg","timestamp":"2026-05-23T02:01:55.865Z",'
    '"payload":{"type":"token_count","cwd":"D:/github","info":{'
    '"total_token_usage":{"input_tokens":37372,"cached_input_tokens":31360,'
    '"output_tokens":828,"reasoning_output_tokens":94},'
    '"last_token_usage":{"input_tokens":12730,"cached_input_tokens":9600,'
    '"output_tokens":137,"reasoning_output_tokens":30}}}}\n'
)


def test_codex_usage_to_tokens(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(CODEX_USAGE, encoding="utf-8")
    meta, evs, tokens = CodexAdapter().parse(str(f), "codex:r")
    assert len(tokens) == 1
    t = tokens[0]
    # 必须采 last_token_usage(per-turn 增量),不是 total_token_usage(session 累积,会虚高 ~120x)
    assert t["input_tokens"] == 12730 and t["cache_read_tokens"] == 9600
    assert t["reasoning_tokens"] == 30


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


# ── opencode: 三层 JSON(session/message/part)解析 ────────────────────────────
# PII 红线:合成 fixture,绝不内联真实对话。

def _build_opencode_tree(storage, sid="ses_test", project_id="proj1",
                         directory="D:/github/story-lifecycle"):
    """在 storage/ 下造一棵 opencode 三层 JSON 树,返回 session.json 路径。"""
    import json, os
    sess_dir = os.path.join(storage, "session", project_id)
    msg_dir = os.path.join(storage, "message", sid)
    part_root = os.path.join(storage, "part")
    os.makedirs(sess_dir, exist_ok=True); os.makedirs(msg_dir, exist_ok=True); os.makedirs(part_root, exist_ok=True)
    # session.json
    sess_f = os.path.join(sess_dir, sid + ".json")
    with open(sess_f, "w", encoding="utf-8") as fh:
        json.dump({
            "id": sid, "directory": directory,
            "summary": "fix login bug",
            "model": "anthropic/claude-sonnet",
            "time": {"created": "2026-07-25T10:00:00.000Z", "updated": "2026-07-25T10:05:00.000Z"},
        }, fh)
    # message m1 (user) + part text
    m1 = "msg1"
    with open(os.path.join(msg_dir, m1 + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": m1, "role": "user", "time_created": "2026-07-25T10:00:01.000Z"}, fh)
    os.makedirs(os.path.join(part_root, m1), exist_ok=True)
    with open(os.path.join(part_root, m1, "p1.json"), "w", encoding="utf-8") as fh:
        json.dump({"type": "text", "text": "实现登录接口并补单测"}, fh)
    # message m2 (assistant) + parts: text, tool, reasoning
    m2 = "msg2"
    with open(os.path.join(msg_dir, m2 + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": m2, "role": "assistant", "modelID": "anthropic/claude-sonnet",
                   "time_created": "2026-07-25T10:01:00.000Z"}, fh)
    os.makedirs(os.path.join(part_root, m2), exist_ok=True)
    with open(os.path.join(part_root, m2, "p2.json"), "w", encoding="utf-8") as fh:
        json.dump({"type": "reasoning", "text": "考虑用 bcrypt"}, fh)
    with open(os.path.join(part_root, m2, "p3.json"), "w", encoding="utf-8") as fh:
        json.dump({"type": "tool", "tool": "bash",
                   "input": {"command": "npm test"},
                   "state": {"status": "completed", "output": "all passed"}}, fh)
    with open(os.path.join(part_root, m2, "p4.json"), "w", encoding="utf-8") as fh:
        json.dump({"type": "text", "text": "已完成"}, fh)
    # token usage on a part (防御式字段名 input/output)
    with open(os.path.join(part_root, m2, "p5.json"), "w", encoding="utf-8") as fh:
        json.dump({"type": "text", "text": "x",
                   "usage": {"input": 100, "output": 40, "cache_read": 60}}, fh)
    return sess_f


def test_opencode_parse_three_layer(tmp_path, monkeypatch):
    from miner.adapters.opencode import OpencodeAdapter
    storage = str(tmp_path / "storage")
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    sess_f = _build_opencode_tree(storage)

    meta, evs, tokens = OpencodeAdapter().parse(sess_f, "opencode:ses_test")

    # meta:cwd → ws,title,ts
    # ws_of 对 D:/github/story-lifecycle 命中 'github' 关键词(WS_KEYWORDS 先匹配)。
    assert meta is not None
    assert meta["src"] == "opencode"
    assert meta["cwd"] == "D:/github/story-lifecycle"
    assert meta["ws"] == "github"
    assert meta["title"] == "fix login bug"
    assert meta["ts"] == "2026-07-25T10:00:00.000Z"
    # turns:1 个真实 user 指令
    assert meta["turns"] == 1
    assert meta["first_ucmd"].startswith("实现登录接口")
    # events:ucmd + think + tool + result + atext(+ 末尾 usage part 的 text)
    kinds = [e["kind"] for e in evs]
    assert "ucmd" in kinds and "think" in kinds and "tool" in kinds and "result" in kinds and "atext" in kinds
    tool_ev = next(e for e in evs if e["kind"] == "tool")
    assert tool_ev["name"] == "bash"
    assert tool_ev["cmd"] == "npm test"
    res_ev = next(e for e in evs if e["kind"] == "result")
    assert res_ev["ok"] is True
    # token:防御式解析 input/output/cache_read
    assert len(tokens) == 1
    assert tokens[0]["input_tokens"] == 100
    assert tokens[0]["output_tokens"] == 40
    assert tokens[0]["cache_read_tokens"] == 60
    assert tokens[0]["model"] == "anthropic/claude-sonnet"


def test_opencode_parse_no_messages_only_meta(tmp_path, monkeypatch):
    """只有 session.json、无 message 目录 → 返回 meta(无事件),不崩。"""
    import json, os
    from miner.adapters.opencode import OpencodeAdapter
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    storage = str(tmp_path / "storage")
    sess_dir = os.path.join(storage, "session", "p1")
    os.makedirs(sess_dir)
    sess_f = os.path.join(sess_dir, "ses_only.json")
    with open(sess_f, "w", encoding="utf-8") as fh:
        json.dump({"id": "ses_only", "directory": "D:/x", "summary": "empty",
                   "time": {"created": "2026-07-25T11:00:00.000Z"}}, fh)

    meta, evs, tokens = OpencodeAdapter().parse(sess_f, "opencode:ses_only")
    assert meta is not None
    assert meta["title"] == "empty"
    assert evs == [] and tokens == []


def test_opencode_discover_yields_session_jsons(tmp_path, monkeypatch):
    from miner.adapters.opencode import OpencodeAdapter
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    storage = str(tmp_path / "storage")
    _build_opencode_tree(storage, sid="ses_a")
    _build_opencode_tree(storage, sid="ses_b", project_id="proj2")

    found = list(OpencodeAdapter().discover())
    sids = sorted(sid for _, sid in found)
    assert sids == ["opencode:ses_a", "opencode:ses_b"]
    assert all(p.endswith(".json") for p, _ in found)

