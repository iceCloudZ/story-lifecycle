"""OpenCode adapter (sst/opencode)。
源: <data>/storage/session/<projectID>/<sessionID>.json (+ 同目录树 message/part)
opencode 把会话拆成三层 JSON 文件:
  storage/session/<projectID>/<sid>.json   会话元信息(directory/time/summary/cost)
  storage/message/<sid>/<msgID>.json       每条消息(role/time_created)
  storage/part/<msgID>/<partID>.json       消息内容分片(text/tool/reasoning/...)
discover yield session.json,sid='opencode:<sid>'。parse 读该 session.json 取 meta,
再去 message/<sid>/ + part/<msgID>/ 还原事件流。mtime 增量以 session.json 为准
(消息更新会刷 session.json 的 time.updated)。token 字段名 opencode 各版本未稳定,
此处防御式解析(message/part 上 usage-like 字段都试),无则返回 []。
"""
import os, glob, json, logging
from .. import common
from ..base import SourceAdapter, register_adapter

log = logging.getLogger(__name__)


def _data_dir():
    """opencode 的 per-user 数据根目录(与 story-lifecycle 侧 _opencode_data_dir 同源)。

    Linux ~/.local/share/opencode / macOS ~/Library/Application Support/opencode /
    Windows %LOCALAPPDATA%/opencode。可被 OPENCODE_DATA_DIR 覆盖。
    """
    override = os.environ.get('OPENCODE_DATA_DIR')
    if override:
        return os.path.expanduser(override)
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
        return os.path.join(base, 'opencode')
    import platform
    if platform.system() == 'Darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'opencode')
    return os.path.join(os.path.expanduser('~'), '.local', 'share', 'opencode')


def _storage_dir():
    return os.path.join(_data_dir(), 'storage')


def _extract_token(part, sid, ts, model):
    """防御式解析 part/message 上的 token usage。opencode 各版本字段名未稳定;
    message 层有 tokens/cost,part 层偶有 usage。识别到任一非零就记一行。"""
    u = part.get('usage') or part.get('tokens') or part.get('tokenUsage')
    if not isinstance(u, dict):
        return None
    inp = (u.get('input') or u.get('input_tokens') or u.get('inputTokens') or 0)
    out = (u.get('output') or u.get('output_tokens') or u.get('outputTokens') or 0)
    cr = (u.get('cache_read') or u.get('cacheRead') or u.get('cached_input_tokens') or 0)
    cc = (u.get('cache_creation') or u.get('cacheCreation') or u.get('cache_creation_input_tokens') or 0)
    rt = (u.get('reasoning') or u.get('reasoning_tokens') or u.get('reasoning_output_tokens') or 0)
    if not any((inp, out, cr, cc, rt)):
        return None
    return dict(sid=sid, src='opencode', ts=ts, model=model or '',
                input_tokens=int(inp), output_tokens=int(out),
                cache_read_tokens=int(cr), cache_creation_tokens=int(cc),
                reasoning_tokens=int(rt))


@register_adapter
class OpencodeAdapter(SourceAdapter):
    name = 'opencode'
    label = 'OpenCode'

    def discover(self):
        # session/<projectID>/<sid>.json;sid 稳定 = 文件名。
        pattern = os.path.join(_storage_dir(), 'session', '*', '*.json')
        for f in glob.glob(pattern):
            if os.path.exists(f):
                yield f, 'opencode:' + os.path.basename(f)[:-5]

    def parse(self, f, sid):
        meta = dict(sid=sid, src='opencode', ws='?', ts=None, title=None, turns=0,
                    ntools=0, nerrs=0, cwd=None, branch=None, first_ucmd=None)
        evs = []
        tokens = []
        storage = _storage_dir()
        try:
            info = json.load(open(f, encoding='utf-8'))
        except Exception as e:
            log.warning("opencode parse failed for %s: %s", f, e)
            return None, [], []
        try:
            # meta from session.json
            sid_raw = info.get('id') or os.path.basename(f)[:-5]
            directory = info.get('directory')
            if directory:
                meta['cwd'] = directory; meta['ws'] = common.ws_of(directory)
            t = info.get('time') or {}
            created = t.get('created')
            if created: meta['ts'] = str(created)
            if info.get('summary'): meta['title'] = str(info['summary'])[:80]
            elif info.get('title'): meta['title'] = str(info['title'])[:80]
            sess_model = info.get('model') or ''

            # messages: storage/message/<sid>/<msgID>.json
            msg_dir = os.path.join(storage, 'message', sid_raw)
            if not os.path.isdir(msg_dir):
                return meta, evs, tokens  # 只有 session 元信息,无消息体
            part_root = os.path.join(storage, 'part')

            # 按 id 排序(opencode id 是 ULID-ish,字典序≈时间序)
            msg_files = sorted(glob.glob(os.path.join(msg_dir, '*.json')))
            for mf in msg_files:
                try:
                    msg = json.load(open(mf, encoding='utf-8'))
                except Exception:
                    continue
                msg_id = msg.get('id') or os.path.basename(mf)[:-5]
                role = (msg.get('role') or '').lower()
                mts = msg.get('time_created') or msg.get('time') or ''
                model = msg.get('modelID') or msg.get('modelID') or sess_model or ''

                # parts: storage/part/<msgID>/<partID>.json
                pdir = os.path.join(part_root, msg_id)
                part_files = sorted(glob.glob(os.path.join(pdir, '*.json'))) if os.path.isdir(pdir) else []
                for pf in part_files:
                    try:
                        part = json.load(open(pf, encoding='utf-8'))
                    except Exception:
                        continue
                    ptype = part.get('type', '')
                    pts = (part.get('time') or {}).get('created') or mts or ''
                    # token(防御式):任何 part/message 带 usage 都记
                    tk = _extract_token(part, sid, pts, model) or _extract_token(msg, sid, mts, model)
                    if tk: tokens.append(tk)
                    if ptype == 'text':
                        txt = part.get('text') or ''
                        if not txt: continue
                        if role == 'user' and common.real_user(txt):
                            meta['turns'] += 1
                            if not meta['first_ucmd']: meta['first_ucmd'] = txt[:160]
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=pts,
                                            kind='ucmd', text=common.mask(txt[:600])))
                        elif role == 'assistant':
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=pts,
                                            kind='atext', text=common.mask(txt[:600])))
                    elif ptype == 'reasoning':
                        txt = part.get('text') or part.get('reasoning') or ''
                        if txt:
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=pts,
                                            kind='think', text=common.mask(txt[:600])))
                    elif ptype == 'tool':
                        nm = part.get('tool') or part.get('name') or '?'
                        meta['ntools'] += 1
                        ev = dict(sid=sid, src='opencode', ws=meta['ws'], ts=pts, kind='tool', name=str(nm))
                        inp = part.get('input') or {}
                        if isinstance(inp, dict):
                            if inp.get('command'): ev['cmd'] = common.mask(str(inp['command'])[:200])
                            if inp.get('path') or inp.get('file_path'): ev['path'] = str(inp.get('path') or inp.get('file_path'))
                        evs.append(ev)
                        # tool 结果(state)
                        st = part.get('state') or {}
                        out = st.get('output')
                        if out is not None or st.get('status'):
                            status = st.get('status', '')
                            ok = status != 'error' if status else True
                            if not ok: meta['nerrs'] += 1
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=pts,
                                kind='result', ok=ok, text=common.mask(str(out)[:200])))
        except Exception as e:
            log.warning("opencode parse failed for %s: %s", f, e)
            return None, [], []
        return meta, evs, tokens
