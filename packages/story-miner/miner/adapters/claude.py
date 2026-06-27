"""Claude Code adapter。
源: ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl，每行一个事件。
sid = 'claude:<session-uuid>'（文件名，稳定）。"""
import os, glob, json
from .. import common, config
from ..base import SourceAdapter, register_adapter

ENCODINGS = config.CLAUDE_ENCODINGS

@register_adapter
class ClaudeAdapter(SourceAdapter):
    name = 'claude'
    label = 'Claude Code'

    def discover(self):
        for enc in ENCODINGS:
            for f in glob.glob(os.path.expanduser(f'~/.claude/projects/{enc}/*.jsonl')):
                if os.path.exists(f):
                    yield f, 'claude:' + os.path.basename(f)[:-6]

    def parse(self, f, sid):
        meta = dict(sid=sid, src='claude', ws='?', ts=None, title=None, turns=0,
                    ntools=0, nerrs=0, cwd=None, branch=None, first_ucmd=None)
        evs = []
        try:
            for line in open(f, encoding='utf-8', errors='ignore'):
                line = line.strip()
                if not line: continue
                try: o = json.loads(line)
                except: continue
                t = o.get('type')
                line_ts = common.full_ts(o)
                if line_ts and not meta['ts']: meta['ts'] = line_ts
                if t == 'ai-title' and o.get('aiTitle'): meta['title'] = o['aiTitle']
                if t == 'last-prompt' and o.get('lastPrompt') and not meta['first_ucmd']:
                    meta['first_ucmd'] = o['lastPrompt'][:160]
                if o.get('cwd'): meta['cwd'] = o['cwd']; meta['ws'] = common.ws_of(o['cwd'])
                if o.get('gitBranch'): meta['branch'] = o['gitBranch']
                msg = o.get('message')
                if not isinstance(msg, dict): continue
                role = msg.get('role'); content = msg.get('content')
                if isinstance(content, str): content = [{'type': 'text', 'text': content}]
                if not isinstance(content, list): continue
                for p in content:
                    if not isinstance(p, dict): continue
                    pt = p.get('type')
                    if pt == 'text':
                        tx = p.get('text', '')
                        if role == 'user' and common.real_user(tx):
                            meta['turns'] += 1
                            if not meta['first_ucmd']: meta['first_ucmd'] = tx[:160]
                            evs.append(dict(sid=sid, src='claude', ws=meta['ws'], ts=line_ts, kind='ucmd', text=common.mask(tx[:600])))
                        elif role == 'assistant':
                            evs.append(dict(sid=sid, src='claude', ws=meta['ws'], ts=line_ts, kind='atext', text=common.mask(tx[:600])))
                    elif pt == 'tool_use':
                        nm = p.get('name', '?'); inp = p.get('input') or {}; meta['ntools'] += 1
                        ev = dict(sid=sid, src='claude', ws=meta['ws'], ts=line_ts, kind='tool', name=nm)
                        if nm == 'Bash':
                            ev['cmd'] = common.mask((inp.get('command', '') or '')[:200])
                        elif nm in ('Read', 'Write', 'Edit', 'NotebookEdit'):
                            ev['path'] = inp.get('file_path', '')
                        elif nm == 'Grep':
                            ev['path'] = inp.get('path') or inp.get('glob') or ''
                            ev['cmd'] = (inp.get('pattern', '') or '')[:80]
                        elif nm == 'Glob':
                            ev['path'] = inp.get('path') or ''
                            ev['cmd'] = (inp.get('pattern', '') or '')[:80]
                        elif nm.startswith('mcp__'):
                            ev['cmd'] = str(inp)[:100]
                        evs.append(ev)
                        if nm in ('Edit', 'Write', 'NotebookEdit'):
                            code = inp.get('new_string') or inp.get('content') or ''
                            if code: evs.append(dict(sid=sid, src='claude', ws=meta['ws'], ts=line_ts, kind='code', name=nm, code=common.mask(code[:1500]), path=inp.get('file_path', '')))
                    elif pt == 'tool_result':
                        c = p.get('content'); txt = ''
                        if isinstance(c, str): txt = c
                        elif isinstance(c, list):
                            for q in c:
                                if isinstance(q, dict) and q.get('type') == 'text': txt += q.get('text', '')
                        ok = not p.get('is_error')
                        if not ok: meta['nerrs'] += 1
                        evs.append(dict(sid=sid, src='claude', ws=meta['ws'], ts=line_ts, kind='result', ok=ok, text=common.mask(txt[:200])))
        except Exception:
            pass
        return meta, evs, []
