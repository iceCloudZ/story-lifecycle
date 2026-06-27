"""Codex CLI adapter。
源: ~/.codex/sessions/YYYY/**/*.jsonl + ~/.codex/archived_sessions/rollout-*.jsonl
每行 {payload, timestamp, type}。sid = 'codex:<filename-without-ext>'（稳定）。"""
import os, glob, json
from .. import common
from ..base import SourceAdapter, register_adapter

@register_adapter
class CodexAdapter(SourceAdapter):
    name = 'codex'
    label = 'Codex CLI'

    def discover(self):
        files = glob.glob(os.path.expanduser('~/.codex/sessions/2026/**/*.jsonl'), recursive=True)
        files += glob.glob(os.path.expanduser('~/.codex/archived_sessions/*.jsonl'))
        for f in files:
            if os.path.exists(f):
                yield f, 'codex:' + os.path.basename(f).replace('.jsonl', '')

    def parse(self, f, sid):
        meta = dict(sid=sid, src='codex', ws='?', ts=None, title=None, turns=0,
                    ntools=0, nerrs=0, cwd=None, branch=None, first_ucmd=None)
        evs = []
        try:
            for line in open(f, encoding='utf-8', errors='ignore'):
                line = line.strip()
                if not line: continue
                try: o = json.loads(line)
                except: continue
                ts = o.get('timestamp')
                if ts and not meta['ts']: meta['ts'] = str(ts)[:10]
                pl = o.get('payload')
                if not isinstance(pl, dict): continue
                pt = pl.get('type'); cwd = pl.get('cwd') or pl.get('workdir')
                if cwd: meta['cwd'] = cwd; meta['ws'] = common.ws_of(cwd)
                if pt in ('message', 'user_message', 'agent_message', 'reasoning'):
                    txt = ''
                    for k in ('content', 'text', 'message', 'reasoning_summary'):
                        v = pl.get(k)
                        if isinstance(v, str): txt += v
                        elif isinstance(v, list):
                            for q in v:
                                if isinstance(q, dict): txt += q.get('text', '') or q.get('output_text', '') or ''
                    role = pl.get('role') or ('user' if pt == 'user_message' else ('assistant' if pt in ('agent_message', 'reasoning') else 'user'))
                    if 'user' in str(role).lower() and common.real_user(txt):
                        meta['turns'] += 1
                        if not meta['first_ucmd']: meta['first_ucmd'] = txt[:160]
                        evs.append(dict(sid=sid, src='codex', ws=meta['ws'], ts=meta['ts'], kind='ucmd', text=common.mask(txt[:600])))
                    elif 'assistant' in str(role).lower() and txt:
                        evs.append(dict(sid=sid, src='codex', ws=meta['ws'], ts=meta['ts'], kind='atext', text=common.mask(txt[:600])))
                elif pt in ('function_call', 'custom_tool_call', 'mcp_tool_call', 'local_shell_call'):
                    nm = pl.get('name', '?'); meta['ntools'] += 1
                    ev = dict(sid=sid, src='codex', ws=meta['ws'], ts=meta['ts'], kind='tool', name=nm)
                    args = pl.get('arguments', '{}'); ad = json.loads(args) if isinstance(args, str) else (args or {})
                    if isinstance(ad, dict) and ad.get('command'): ev['cmd'] = common.mask(ad['command'][:200])
                    evs.append(ev)
                elif pt in ('function_call_output', 'custom_tool_call_output', 'mcp_tool_call_output'):
                    out = pl.get('output', ''); out_s = str(out)
                    import re
                    m = re.search(r'exit code:\s*(\d+)', out_s.lower())
                    ok = (m.group(1) == '0') if m else not any(e in out_s.lower() for e in ['traceback', 'exception', 'error:'])
                    if not ok: meta['nerrs'] += 1
                    evs.append(dict(sid=sid, src='codex', ws=meta['ws'], ts=meta['ts'], kind='result', ok=ok, text=common.mask(str(out)[:200])))
        except Exception:
            pass
        return meta, evs
