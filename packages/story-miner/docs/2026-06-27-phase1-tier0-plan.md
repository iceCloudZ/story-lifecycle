# 一期 Tier 0:原生数据梳理 — Implementation Plan

> **For agentic workers:** 用 superpowers:executing-plans 或 subagent-driven-development 逐任务执行。步骤用 `- [ ]` 跟踪。
> **上下文:** spec 见同目录 `2026-06-27-phase1-tier0-native-data-design.md`。本计划自包含;token 字段路径不确定处,每个任务有"对样本 pin"步骤,务必先 grep 真实样本确认。
> **PII 红线:** transcript 含金融 PII。测试只用合成 sanitized fixture,绝不内联真实对话内容。

**Goal:** 修三端 adapter 的 timestamp 截断 / Kimi 丢失败信号 / token 错位缺失,建 `token_usage` 表支持 per-story token 与效率聚合。

**Architecture:** 扩展 `SourceAdapter.parse()` → `(meta, events, tokens)`;新增 `token_usage` 表(每 turn 一行,覆盖所有 session);`scripts/story_token.py` 出 per-story / per-session / 未关联三层聚合 + cache 命中率 / out-in 比。

**Tech Stack:** Python 3.10+、SQLite、pytest。包根 `packages/story-miner`(路径相对仓库根 `D:/github/story-lifecycle`)。

**运行环境(仓库根执行):**
```bash
source .venv-monorepo-test/Scripts/activate   # 或现有 venv
export PYTHONPATH=packages/story-miner        # 让 `import miner` 可用
```
单测:`python -m pytest packages/story-miner/tests -q`

---

## 文件结构

| 文件 | 改动 | 职责 |
|---|---|---|
| `packages/story-miner/miner/base.py` | 改 | `parse()` 返回 `(meta, events, tokens)` |
| `packages/story-miner/miner/common.py` | 改 | 加 `full_ts(o)` 时间戳归一 helper |
| `packages/story-miner/miner/adapters/claude.py` | 改 | per-event ts;assistant `usage`→tokens |
| `packages/story-miner/miner/adapters/codex.py` | 改 | per-event ts;`token_count`→tokens |
| `packages/story-miner/miner/adapters/kimi.py` | 改 | per-event ts;`usage.record`→tokens(不再进 think);`isError`→result |
| `packages/story-miner/miner/store.py` | 改 | SCHEMA 加 `token_usage`;insert + 删连带 |
| `packages/story-miner/scripts/story_token.py` | 新 | 三层聚合 + 效率列 |
| `packages/story-miner/tests/test_adapters.py` | 新 | adapter 单测(synthetic fixture) |

---

## Task 1: 扩展 parse() 契约返回 (meta, events, tokens)

接口改三元组,所有 adapter 暂返空 tokens,store 解包。本任务不改行为,只铺路。

**Files:** Modify `miner/base.py`, `miner/adapters/{claude,codex,kimi}.py`, `miner/store.py`

- [ ] **Step 1: 写失败测试** — 新建 `tests/test_adapters.py`
```python
import json
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
```

- [ ] **Step 2: 跑,确认失败** — `python -m pytest packages/story-miner/tests/test_adapters.py -q` → FAIL(ValueError: too many values / 2-tuple)

- [ ] **Step 3: 改 base.py** — `parse` 返回值契约改三元组
```python
# miner/base.py 的 SourceAdapter.parse docstring 末尾改成:
    def parse(self, path, sid):
        """... -> (meta_dict, [event_dict,...], [token_dict,...])。
        token_dict: {sid,src,ts,model,input_tokens,output_tokens,
                     cache_read_tokens,cache_creation_tokens,reasoning_tokens}。
        不支持 token 的端返回 []。"""
        return {}, [], []
```

- [ ] **Step 4: 三 adapter 末尾改返回** — 各自 `return meta, evs` → `return meta, evs, []`(在 parse 末尾、`return meta, evs` 处)

- [ ] **Step 5: store.py 解包三元组** — `main()` 里 `meta, evs = ad.parse(path, sid)` → `meta, evs, _tokens = ad.parse(path, sid)`(本任务先忽略 tokens)

- [ ] **Step 6: 跑全量测试** — `python -m pytest packages/story-miner/tests -q` → 全 PASS(含现有 3 测试不回归)

- [ ] **Step 7: 提交** — `git add -A && git commit -m "refactor(miner): parse() returns (meta, events, tokens)"`

---

## Task 2: common.full_ts + per-event 全 ISO 时间戳

**Files:** Modify `miner/common.py`, `miner/adapters/{claude,codex,kimi}.py`

- [ ] **Step 1: 写失败测试**(加到 `test_adapters.py`)
```python
from miner import common

def test_full_ts_iso():
    assert common.full_ts({"timestamp": "2026-06-27T10:00:00.5Z"}).startswith("2026-06-27T10:00:00")

def test_full_ts_ms():
    # 1781688000000 ms -> 2026-... ISO,非空且含 'T'
    s = common.full_ts({"time": 1781688000000})
    assert s and "T" in s and len(s) >= 19

def test_full_ts_fallback():
    assert common.full_ts({}, "FB") == "FB"
```

- [ ] **Step 2: 跑,确认失败** → FAIL(`full_ts` 不存在)

- [ ] **Step 3: common.py 加 helper**
```python
import datetime

def full_ts(o, fallback=''):
    """从事件 dict 提取完整 ISO 时间戳(Claude/Codex 的 timestamp;Kimi 的 time 毫秒)。"""
    ts = o.get('timestamp')
    if ts:
        return str(ts)
    t_ms = o.get('time')
    if t_ms:
        try:
            return datetime.datetime.fromtimestamp(
                t_ms / 1000, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(t_ms) % 1000:03d}Z'
        except Exception:
            return fallback
    return fallback
```

- [ ] **Step 4: 三 adapter 改 per-event ts**
  - **claude.py**:删 `ts = o.get('timestamp'); if ts and not meta['ts']: meta['ts'] = str(ts)[:10]`;改为 `line_ts = common.full_ts(o); if line_ts and not meta['ts']: meta['ts'] = line_ts`;所有 `evs.append(dict(... ts=meta['ts'] ...))` 的 `ts=meta['ts']` 换成 `ts=line_ts`(uCMD/atext/tool/code/result 各处)。
  - **codex.py**:`ts = o.get('timestamp')` 那两行同理换成 `line_ts = common.full_ts(o)`;事件 `ts=meta['ts']` 换 `ts=line_ts`。
  - **kimi.py**:删 `t_ms = o.get('time') ... strftime('%Y-%m-%d')` 块;改 `line_ts = common.full_ts(o); if line_ts and not meta['ts']: meta['ts'] = line_ts`;事件 `ts=meta['ts']` 换 `ts=line_ts`。

- [ ] **Step 5: 跑测试** → 全 PASS

- [ ] **Step 6: 提交** — `git commit -am "fix(miner): per-event full-ISO timestamps (was truncated to date)"`

---

## Task 3: token_usage 表 + store 插入/删除连带

**Files:** Modify `miner/store.py`

- [ ] **Step 1: 写失败测试**(加到 `test_adapters.py`)
```python
import sqlite3
from miner import store

def test_token_usage_table_created(tmp_path):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)")]
    conn.close()
    for c in ("sid","src","ts","model","input_tokens","output_tokens",
              "cache_read_tokens","cache_creation_tokens","reasoning_tokens"):
        assert c in cols
```

- [ ] **Step 2: 跑,确认失败** → FAIL(table_token_usage no such table)

- [ ] **Step 3: store.py SCHEMA 加表**(在 SCHEMA 字符串里 events_fts trigger 之后加)
```sql
CREATE TABLE IF NOT EXISTS token_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, src TEXT, ts TEXT, model TEXT,
  input_tokens INT, output_tokens INT,
  cache_read_tokens INT, cache_creation_tokens INT, reasoning_tokens INT);
CREATE INDEX IF NOT EXISTS idx_tu_sid ON token_usage(sid);
```

- [ ] **Step 4: store.py main() 插入 tokens** — Task1 的 `_tokens` 改回 `tokens`;在插入 events 之后、且仅在 session 入库(非 skip)分支内加:
```python
        if tokens:
            trows = [(t.get('sid'), t.get('src'), t.get('ts'), t.get('model'),
                      t.get('input_tokens'), t.get('output_tokens'),
                      t.get('cache_read_tokens'), t.get('cache_creation_tokens'),
                      t.get('reasoning_tokens')) for t in tokens]
            conn.executemany(
                'INSERT INTO token_usage(sid,src,ts,model,input_tokens,output_tokens,'
                'cache_read_tokens,cache_creation_tokens,reasoning_tokens) '
                'VALUES(?,?,?,?,?,?,?,?,?)', trows)
```
  并在三处删 sid 的地方(`DELETE FROM events WHERE sid=?` 旁边)加 `conn.execute('DELETE FROM token_usage WHERE sid=?', (sid,))`(共 2 处:to_up 删 + to_del 删)。

- [ ] **Step 5: 跑测试** → PASS

- [ ] **Step 6: 提交** — `git commit -am "feat(miner): token_usage table + cascade delete"`

---

## Task 4: Claude token 采集

**Files:** Modify `miner/adapters/claude.py`

- [ ] **Step 1: 对样本 pin** — `grep -m1 -o '"usage":{[^}]*}' ~/.claude/projects/D--github/*.jsonl | head -1` 确认字段名(`input_tokens`/`cache_read_input_tokens`/`cache_creation_input_tokens`/`output_tokens`),不符则按实际改 Step 3。

- [ ] **Step 2: 写失败测试**
```python
CLAUDE_USAGE = (
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ok"}],'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":200,'
    '"cache_creation_input_tokens":0,"output_tokens":50}},'
    '"timestamp":"2026-06-27T10:00:01.000Z","cwd":"D:/github"}\n'
)
def test_claude_usage_to_tokens(tmp_path):
    f = tmp_path / "s.jsonl"; f.write_text(CLAUDE_USAGE, encoding="utf-8")
    meta, evs, tokens = ClaudeAdapter().parse(str(f), "claude:s")
    assert len(tokens) == 1
    t = tokens[0]
    assert t["input_tokens"] == 100 and t["cache_read_tokens"] == 200
    assert t["output_tokens"] == 50 and t["src"] == "claude"
```

- [ ] **Step 3: 跑,确认失败** → `len(tokens)==0`

- [ ] **Step 4: claude.py 实现** — parse 顶部 `evs = []` 下加 `tokens = []`;在 content 循环之后(同一 `for p in content` 结束后,仍在 `for line` 内)、`return` 前,加:
```python
                if role == 'assistant':
                    u = msg.get('usage') or {}
                    if u:
                        tokens.append(dict(sid=sid, src='claude', ts=line_ts,
                            model=o.get('model',''),
                            input_tokens=u.get('input_tokens') or 0,
                            output_tokens=u.get('output_tokens') or 0,
                            cache_read_tokens=u.get('cache_read_input_tokens') or 0,
                            cache_creation_tokens=u.get('cache_creation_input_tokens') or 0,
                            reasoning_tokens=u.get('reasoning_tokens') or 0))
```
  末尾 `return meta, evs, []` → `return meta, evs, tokens`。

- [ ] **Step 5: 跑测试** → PASS

- [ ] **Step 6: 提交** — `git commit -am "feat(claude): capture per-turn usage into token_usage"`

---

## Task 5: Codex token 采集

**Files:** Modify `miner/adapters/codex.py`

- [ ] **Step 1: 对样本 pin** — `grep -m1 -o 'total_token_usage":{[^}]*}' ~/.codex/sessions/2026/**/*.jsonl | head -1` 确认字段(`input_tokens`/`cached_input_tokens`/`output_tokens`/`reasoning_output_tokens`)。

- [ ] **Step 2: 写失败测试**
```python
from miner.adapters.codex import CodexAdapter
CODEX_USAGE = (
    '{"type":"event_msg","timestamp":"2026-05-23T02:01:55.865Z",'
    '"payload":{"type":"token_count","cwd":"D:/github","info":{'
    '"total_token_usage":{"input_tokens":12191,"cached_input_tokens":9600,'
    '"output_tokens":343,"reasoning_output_tokens":94}}}}\n'
)
def test_codex_usage_to_tokens(tmp_path):
    f = tmp_path / "r.jsonl"; f.write_text(CODEX_USAGE, encoding="utf-8")
    meta, evs, tokens = CodexAdapter().parse(str(f), "codex:r")
    assert len(tokens) == 1
    t = tokens[0]
    assert t["input_tokens"] == 12191 and t["cache_read_tokens"] == 9600
    assert t["reasoning_tokens"] == 94
```

- [ ] **Step 3: 跑,确认失败** → `len(tokens)==0`

- [ ] **Step 4: codex.py 实现** — 顶部加 `tokens = []`;在 `elif pt in ('function_call_output', ...)` 分支之后加:
```python
                elif pt == 'token_count':
                    u = (pl.get('info') or {}).get('total_token_usage') or {}
                    if u:
                        tokens.append(dict(sid=sid, src='codex', ts=line_ts,
                            model=pl.get('model','') or o.get('model',''),
                            input_tokens=u.get('input_tokens') or 0,
                            output_tokens=u.get('output_tokens') or 0,
                            cache_read_tokens=u.get('cached_input_tokens') or 0,
                            cache_creation_tokens=u.get('cache_creation_input_tokens') or 0,
                            reasoning_tokens=u.get('reasoning_output_tokens') or 0))
```
  末尾 `return meta, evs, []` → `return meta, evs, tokens`。

- [ ] **Step 5: 跑测试** → PASS

- [ ] **Step 6: 提交** — `git commit -am "feat(codex): capture token_count into token_usage"`

---

## Task 6: Kimi token 采集 + think 清理

**Files:** Modify `miner/adapters/kimi.py`

- [ ] **Step 1: 对样本 pin** — `grep -m1 -o '"usage":{[^}]*}' ~/.kimi-code/sessions/wd_*/**/wire.jsonl | head -1` 确认 Kimi 字段(`inputOther`/`output`/`inputCacheRead`/`inputCacheCreation`)。

- [ ] **Step 2: 写失败测试**
```python
from miner.adapters.kimi import KimiAdapter
KIMI_USAGE = (
    '{"type":"usage.record","time":1781688000000,"model":"kimi-for-coding",'
    '"usage":{"inputOther":3825,"output":185,"inputCacheRead":14848,'
    '"inputCacheCreation":0}}\n'
)
def test_kimi_usage_to_tokens_not_think(tmp_path):
    f = tmp_path / "w.jsonl"; f.write_text(KIMI_USAGE, encoding="utf-8")
    meta, evs, tokens = KimiAdapter().parse(str(f), "kimi:s:main")
    assert len(tokens) == 1
    assert tokens[0]["output_tokens"] == 185
    assert tokens[0]["cache_read_tokens"] == 14848
    # 不再产生 think 事件
    assert not any(e.get("kind") == "think" for e in evs)
```

- [ ] **Step 3: 跑,确认失败** → `len(tokens)==0`(当前 emit think)

- [ ] **Step 4: kimi.py 实现** — 顶部 `evs = []` 下加 `tokens = []`;把现有 `elif typ == 'usage.record':` 分支整段(原 emit `kind='think'`)替换为:
```python
                elif typ == 'usage.record':
                    u = o.get('usage') or {}
                    if u:
                        tokens.append(dict(sid=sid, src='kimi', ts=line_ts,
                            model=o.get('model',''),
                            input_tokens=(u.get('inputOther') or 0) + (u.get('inputCacheRead') or 0),
                            output_tokens=u.get('output') or 0,
                            cache_read_tokens=u.get('inputCacheRead') or 0,
                            cache_creation_tokens=u.get('inputCacheCreation') or 0,
                            reasoning_tokens=0))
```
  末尾 `return meta, evs, []` → `return meta, evs, tokens`。注意 Kimi parse 用 `line_ts = common.full_ts(o)`(Task 2 已加);若该变量名不同,用 Task 2 引入的名字。

- [ ] **Step 5: 跑测试** → PASS

- [ ] **Step 6: 提交** — `git commit -am "feat(kimi): usage.record -> token_usage; free think kind"`

---

## Task 7: Kimi isError → kind='result'

**Files:** Modify `miner/adapters/kimi.py`

- [ ] **Step 1: 对样本 pin** — 在真实 wire.jsonl 里找一个含 `isError` 的 `context.append_loop_event`,确认 `isError` 的嵌套路径(预期在 `event.result.isError` 或 event 内 result 对象)。命令:`grep -m1 -o 'isError[^,}]*' ~/.kimi-code/sessions/wd_*/**/wire.jsonl | head -3`。按实际路径调 Step 4。

- [ ] **Step 2: 写失败测试**(按 pin 到的路径构造 fixture;下方假设 `event.result.isError`)
```python
KIMI_ERR = (
    '{"type":"context.append_loop_event","time":1781688001000,"event":'
    '{"tool_name":"Bash","result":{"output":"boom","isError":true}}}\n'
)
def test_kimi_iserror_to_result(tmp_path):
    f = tmp_path / "w.jsonl"; f.write_text(KIMI_ERR, encoding="utf-8")
    meta, evs, tokens = KimiAdapter().parse(str(f), "kimi:s:main")
    results = [e for e in evs if e.get("kind") == "result"]
    assert results and results[0]["ok"] == 0
```

- [ ] **Step 3: 跑,确认失败** → 无 result 事件

- [ ] **Step 4: kimi.py 实现** — 在 `context.append_loop_event` 分支里,提取 tool_name 之后加:
```python
                        res = ev.get('result') if isinstance(ev, dict) else None
                        if isinstance(res, dict):
                            meta['ntools'] = meta.get('ntools', 0)  # no-op 占位
                            evs.append(dict(sid=sid, src='kimi', ws=meta['ws'], ts=line_ts,
                                kind='result', ok=0 if res.get('isError') else 1,
                                text=common.mask(str(res.get('output',''))[:200])))
```
  (注意:此处 `ev` 变量名与 Kimi 现有 `ev = o.get('event')` 一致;`line_ts` 同 Task 6。若 isError 实际路径不同,按 Step 1 pin 结果改 `res` 取值。)

- [ ] **Step 5: 跑测试** → PASS

- [ ] **Step 6: 提交** — `git commit -am "feat(kimi): capture isError as kind=result (unblocks failure_mode)"`

---

## Task 8: scripts/story_token.py(三层聚合 + 效率列)

**Files:** Create `packages/story-miner/scripts/story_token.py`

- [ ] **Step 1: 写失败测试**(加到 `test_adapters.py`;构造一个含 token_usage + sessions 的临时 db)
```python
def test_story_token_aggregation(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sessions(sid,story_id) VALUES('claude:a','S1'),('kimi:b',NULL)")
    conn.execute("INSERT INTO sessions(sid,story_id) VALUES('claude:c','S1')")
    conn.execute("INSERT INTO token_usage(sid,src,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens) "
                 "VALUES('claude:a','claude',100,50,200,0),('kimi:b','kimi',40,10,0,0),('claude:c','claude',60,30,100,0)")
    conn.commit(); conn.close()
    import importlib.util, os
    spec = importlib.util.spec_from_file_location("st",
        os.path.join("packages","story-miner","scripts","story_token.py"))
    st = importlib.util.module_from_spec(spec); spec.loader.exec_module(st)
    per_story, unlinked = st.aggregate(db)
    # S1 = a+c: input 160, output 80, cache_read 300
    assert per_story["S1"]["input_tokens"] == 160 and per_story["S1"]["output_tokens"] == 80
    assert per_story["S1"]["cache_read_tokens"] == 300
    assert 0 < per_story["S1"]["cache_hit"] < 1   # 效率列存在
    # 未关联 = b
    assert unlinked["input_tokens"] == 40
```

- [ ] **Step 2: 跑,确认失败** → 模块/函数不存在

- [ ] **Step 3: 写 scripts/story_token.py**
```python
"""per-story / per-session / 未关联 token 聚合 + 效率列(cache 命中率、out/in 比)。
输出 scripts/out/story_token.md。"""
import os, sys, sqlite3, collections

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)
from miner import config

OUT_MD = os.path.join(_PROJ, 'scripts', 'out', 'story_token.md')


def aggregate(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT t.sid, s.story_id, t.input_tokens, t.output_tokens, '
        't.cache_read_tokens, t.cache_creation_tokens, t.reasoning_tokens '
        'FROM token_usage t LEFT JOIN sessions s ON s.sid = t.sid').fetchall()
    conn.close()
    per_story = collections.defaultdict(lambda: collections.Counter())
    unlinked = collections.Counter()
    for sid, story_id, inp, out, cr, cc, reas in rows:
        bucket = per_story[story_id] if story_id else unlinked
        bucket['input_tokens'] += inp or 0
        bucket['output_tokens'] += out or 0
        bucket['cache_read_tokens'] += cr or 0
        bucket['cache_creation_tokens'] += cc or 0
        bucket['reasoning_tokens'] += reas or 0
        bucket['n'] += 1
    def enrich(c):
        inp = c['input_tokens']; denom = inp + c['cache_read_tokens'] + c['cache_creation_tokens']
        c['total_tokens'] = inp + c['output_tokens'] + c['cache_read_tokens'] + c['cache_creation_tokens']
        c['cache_hit'] = (c['cache_read_tokens'] / denom) if denom else 0.0
        c['out_in_ratio'] = (c['output_tokens'] / inp) if inp else 0.0
        return c
    for k in per_story:
        enrich(per_story[k])
    enrich(unlinked)
    return per_story, unlinked


def main():
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    per_story, unlinked = aggregate(config.DB_PATH)
    out = ['# Story Token 消耗\n', '## per-story\n',
           '| story | turns | input | output | cache_read | cache_create | reasoning | total | cache_hit | out/in |',
           '|---|---|---|---|---|---|---|---|---|---|']
    for sid, c in sorted(per_story.items(), key=lambda kv: -kv[1]['total_tokens']):
        out.append(f"| {sid} | {c['n']} | {c['input_tokens']} | {c['output_tokens']} | "
                   f"{c['cache_read_tokens']} | {c['cache_creation_tokens']} | {c['reasoning_tokens']} | "
                   f"{c['total_tokens']} | {c['cache_hit']:.1%} | {c['out_in_ratio']:.2f} |")
    out += ['\n## 未关联 story 的 session(合计)\n',
            f"- turns: {unlinked['n']}  input: {unlinked['input_tokens']}  output: {unlinked['output_tokens']}  "
            f"cache_read: {unlinked['cache_read_tokens']}  total: {unlinked['total_tokens']}  "
            f"cache_hit: {unlinked['cache_hit']:.1%}"]
    with open(OUT_MD, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out))
    print(f"story_token -> {OUT_MD}")
    print(f"stories: {len(per_story)}, unlinked turns: {unlinked['n']}")


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: 跑测试** → PASS

- [ ] **Step 5: 提交** — `git add scripts/story_token.py tests/test_adapters.py && git commit -m "feat: story_token.py per-story/per-session/unlinked + cache_hit"`

---

## Task 9: 迁移(备份 + 全量重建 + 校验)

**Files:** 无(运行手册)

- [ ] **Step 1: 备份** — `cp packages/story-miner/data/transcripts.db packages/story-miner/data/transcripts.db.bak`(若文件很大,确认磁盘空间)

- [ ] **Step 2: 删库重建**
```bash
cd packages/story-miner
rm -f data/transcripts.db
python -m miner.store                  # 全量入库(无 --since-days)
python -m miner.story_ingest           # .story/ -> stories
python -m miner.link                   # session↔story
python scripts/story_token.py          # 产出 scripts/out/story_token.md
```

- [ ] **Step 3: 校验(无回退)**
```bash
python -c "import sqlite3; c=sqlite3.connect('data/transcripts.db'); \
print('sessions by src', dict(c.execute('SELECT src,count(*) FROM sessions GROUP BY src'))); \
print('events', c.execute('SELECT count(*) FROM events').fetchone()[0]); \
print('token_usage', c.execute('SELECT count(*) FROM token_usage').fetchone()[0])"
```
  预期:sessions by src 含 claude/codex/kimi(数量与备份前同量级);token_usage 行数 > 0(≈ 三端 turn 量级)。

- [ ] **Step 4: timestamp 精度 sanity** — 抽一个 session 看 events.ts 是否各异(非全相同):
```bash
python -c "import sqlite3; c=sqlite3.connect('data/transcripts.db'); \
rows=c.execute('SELECT DISTINCT ts FROM events WHERE sid=(SELECT sid FROM sessions LIMIT 1) LIMIT 5').fetchall(); print(rows)"
```
  预期:多行不同 ts(含 T 时分秒)。

- [ ] **Step 5: 看 story_token.md** — 抽 1 个 story + 未关联合计,确认数字合理。

- [ ] **Step 6: 跑全量测试** — `python -m pytest packages/story-miner/tests -q` → 全 PASS。

- [ ] **Step 7: 提交** — `git commit -am "chore(miner): rebuild transcripts.db with token_usage + per-event ts"`(注意:`data/*.db` 已 gitignore,不进 git;此提交仅若有脚本/配置变动)

---

## Self-Review(已自检)

- **Spec 覆盖:** timestamp(Task2)、Kimi isError(Task7)、token_usage+parse契约(Task1/3/4/5/6)、Kimi think 清理(Task6)、story_token 三层(Task8)、未关联覆盖(Task8 unlinked + 迁移校验)—— spec 每条都有任务。
- **占位扫描:** 无 TBD;每 token 任务有"对样本 pin"步骤(字段名以 grep 实测为准,非占位)。
- **类型一致:** token dict 字段跨 Task 3/4/5/6 一致(input_tokens/output_tokens/cache_read_tokens/cache_creation_tokens/reasoning_tokens);`line_ts` 变量名跨 Task 2/4/5/6 一致。
- **效率列:** Task 8 含 cache_hit / out_in_ratio(回应用户的 token 效率诉求)。
