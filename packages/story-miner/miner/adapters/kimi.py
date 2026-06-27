"""Kimi-Code adapter。
源: ~/.kimi-code/sessions/wd_<cwd>_<hash>/session_<uuid>/agents/<agent>/wire.jsonl
事件类型: turn.prompt(用户回合, input[]) | context.append_message(助手文本)
        | context.append_loop_event(工具循环) | usage.record(token)
time 为 epoch 毫秒。sid='kimi:<session_uuid>:<agent>'（多 agent 会话各自独立）。"""
import os, glob, json
from .. import common
from ..base import SourceAdapter, register_adapter

@register_adapter
class KimiAdapter(SourceAdapter):
    name = 'kimi'
    label = 'Kimi Code'

    def discover(self):
        for f in glob.glob(os.path.expanduser('~/.kimi-code/sessions/wd_*/**/wire.jsonl'), recursive=True):
            if os.path.exists(f):
                # .../wd_<cwd>_<hash>/session_<uuid>/agents/<agent>/wire.jsonl
                parts = f.replace('\\', '/').split('/')
                sess = next((p for p in reversed(parts) if p.startswith('session_')), 'unknown')
                agent = parts[-2] if len(parts) >= 2 else 'main'
                yield f, f'kimi:{sess.replace("session_", "")}:{agent}'

    def parse(self, f, sid):
        meta = dict(sid=sid, src='kimi', ws='?', ts=None, title=None, turns=0,
                    ntools=0, nerrs=0, cwd=None, branch=None, first_ucmd=None)
        evs = []
        tokens = []
        # ws 从 wd_<cwd>_<hash> 目录还原
        wd_dir = next((p for p in f.replace('\\', '/').split('/') if p.startswith('wd_')), '')
        if wd_dir:
            cwd_guess = wd_dir.split('_', 1)[-1].rsplit('_', 1)[0]
            meta['ws'] = common.ws_of(cwd_guess); meta['cwd'] = cwd_guess
        try:
            for line in open(f, encoding='utf-8', errors='ignore'):
                line = line.strip()
                if not line: continue
                try: o = json.loads(line)
                except: continue
                line_ts = common.full_ts(o)
                if line_ts and not meta['ts']: meta['ts'] = line_ts
                typ = o.get('type', '')
                if typ == 'turn.prompt':
                    inp = o.get('input'); txt = ''
                    if isinstance(inp, list):
                        for q in inp:
                            if isinstance(q, dict): txt += q.get('text', '') or ''
                    elif isinstance(inp, str): txt = inp
                    if common.real_user(txt):
                        meta['turns'] += 1
                        if not meta['first_ucmd']: meta['first_ucmd'] = txt[:160]
                        evs.append(dict(sid=sid, src='kimi', ws=meta['ws'], ts=line_ts, kind='ucmd', text=common.mask(txt[:600])))
                elif typ == 'context.append_message':
                    msg = o.get('message')
                    if isinstance(msg, dict):
                        role = msg.get('role'); c = msg.get('content'); txt = ''
                        if isinstance(c, str): txt = c
                        elif isinstance(c, list):
                            for q in c:
                                if isinstance(q, dict): txt += q.get('text', '') or ''
                        if role == 'assistant' and txt:
                            evs.append(dict(sid=sid, src='kimi', ws=meta['ws'], ts=line_ts, kind='atext', text=common.mask(txt[:600])))
                elif typ == 'context.append_loop_event':
                    ev = o.get('event')
                    if isinstance(ev, dict):
                        nm = ev.get('tool_name') or ev.get('name') or ev.get('tool') or ''
                        if nm and nm not in ('message', ''):
                            meta['ntools'] += 1
                            evs.append(dict(sid=sid, src='kimi', ws=meta['ws'], ts=line_ts, kind='tool', name=str(nm)))
                        res = ev.get('result')
                        if isinstance(res, dict):
                            evs.append(dict(sid=sid, src='kimi', ws=meta['ws'], ts=line_ts,
                                kind='result', ok=0 if res.get('isError') else 1,
                                text=common.mask(str(res.get('output', ''))[:200])))
                elif typ == 'usage.record':
                    u = o.get('usage') or {}
                    if u:
                        tokens.append(dict(sid=sid, src='kimi', ts=line_ts,
                            model=o.get('model', ''),
                            input_tokens=(u.get('inputOther') or 0) + (u.get('inputCacheRead') or 0),
                            output_tokens=u.get('output') or 0,
                            cache_read_tokens=u.get('inputCacheRead') or 0,
                            cache_creation_tokens=u.get('inputCacheCreation') or 0,
                            reasoning_tokens=0))
                if o.get('summary') and not meta['title']: meta['title'] = str(o['summary'])[:80]
        except Exception:
            pass
        return meta, evs, tokens
