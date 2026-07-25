"""OpenCode adapter (sst/opencode,1.18+)。
源: <data>/opencode.db (SQLite 单文件,取代旧版三层 JSON 文件布局)
表:
  session(id, directory, title, model, tokens_input/output/reasoning/cache_read/cache_write,
          time_created, time_updated, ...)
  message(id, session_id, time_created, data JSON)   data: {role, agent, model, cost, ...}
  part(id, message_id, session_id, time_created, data JSON)
       data.type: text/tool/reasoning/step-start/step-finish
       tool: {tool, state:{status, input, output}}
discover: 连 db,遍历 session 表 yield (sid_rowid, 'opencode:<id>')。
  注:store 的增量以 sources 表 path+mtime 为准 —— opencode.db 是单文件,其 mtime 变化
  即代表有更新,所以 path 固定为 db 路径、mtime 取 db 文件 mtime。parse 内部按 sid 自查。
parse(path, sid): path=opencode.db 路径;sid='opencode:<session_id>'。查 session 取 meta,
  再查 message+part 还原事件流。token 直接读 session 表列(不用解析 part)。
"""
import os, json, sqlite3, logging
from .. import common
from ..base import SourceAdapter, register_adapter

log = logging.getLogger(__name__)


def _data_dir():
    """opencode 的 per-user 数据根目录(与 story-lifecycle 侧 _opencode_data_dir 同源)。

    Linux ~/.local/share/opencode / macOS ~/Library/Application Support/opencode /
    Windows 实测也是 ~/.local/share/opencode(Linux 风格,不是 %LOCALAPPDATA%)。
    可被 OPENCODE_DATA_DIR 覆盖。
    """
    override = os.environ.get('OPENCODE_DATA_DIR')
    if override:
        return os.path.expanduser(override)
    import platform
    if platform.system() == 'Darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'opencode')
    return os.path.join(os.path.expanduser('~'), '.local', 'share', 'opencode')


def _db_path():
    return os.path.join(_data_dir(), 'opencode.db')


def _connect_ro(path):
    """只读连接 opencode.db(URI mode=ro),绝不干扰 opencode 写入。"""
    p = path.replace('\\', '/')
    return sqlite3.connect(f'file:{p}?mode=ro', uri=True)


@register_adapter
class OpencodeAdapter(SourceAdapter):
    name = 'opencode'
    label = 'OpenCode'

    def discover(self):
        # opencode.db 是单文件;store 的增量逻辑看 sources 表的 (path, mtime)。
        # path 固定 = db 路径,mtime = db 文件 mtime。parse 内部按 sid 自查具体 session。
        db = _db_path()
        if not os.path.exists(db):
            return
        # 遍历所有 session(供 store 按 sid 逐个 upsert)。
        try:
            conn = _connect_ro(db)
            try:
                for (sid,) in conn.execute('SELECT id FROM session').fetchall():
                    yield db, 'opencode:' + sid
            finally:
                conn.close()
        except Exception as e:
            log.warning('opencode discover failed: %s', e)

    def parse(self, f, sid):
        # f = opencode.db 路径;sid = 'opencode:<session_id>'
        meta = dict(sid=sid, src='opencode', ws='?', ts=None, title=None, turns=0,
                    ntools=0, nerrs=0, cwd=None, branch=None, first_ucmd=None)
        evs = []
        tokens = []
        sess_id = sid[len('opencode:'):] if sid.startswith('opencode:') else sid
        if not os.path.exists(f):
            return None, [], []
        try:
            conn = _connect_ro(f)
        except Exception as e:
            log.warning('opencode parse open db failed for %s: %s', f, e)
            return None, [], []
        try:
            row = conn.execute(
                'SELECT id, directory, title, model, tokens_input, tokens_output, '
                'tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created '
                'FROM session WHERE id=?', (sess_id,)).fetchone()
            if not row:
                return None, [], []  # session 已不在(被删)→ 跳过
            (_id, directory, title, model, t_in, t_out, t_reason, t_cr, t_cc, t_created) = row
            if directory:
                meta['cwd'] = directory; meta['ws'] = common.ws_of(directory)
            if title: meta['title'] = str(title)[:80]
            if t_created: meta['ts'] = common.full_ts({'time': t_created})
            # token:opencode 直接在 session 表上累积(整会话),记一行。
            if any((t_in, t_out, t_reason, t_cr, t_cc)):
                # model 列是 JSON: {"id":"...","providerID":"..."};取 id。
                model_id = model or ''
                try:
                    mobj = json.loads(model) if isinstance(model, str) and model.startswith('{') else None
                    if isinstance(mobj, dict): model_id = mobj.get('id') or model_id
                except Exception:
                    pass
                tokens.append(dict(sid=sid, src='opencode', ts=meta['ts'], model=model_id,
                                   input_tokens=int(t_in or 0), output_tokens=int(t_out or 0),
                                   cache_read_tokens=int(t_cr or 0), cache_creation_tokens=int(t_cc or 0),
                                   reasoning_tokens=int(t_reason or 0)))

            # messages + parts:按 time 排序还原事件流。
            msgs = conn.execute(
                'SELECT id, time_created, data FROM message WHERE session_id=? '
                'ORDER BY time_created', (sess_id,)).fetchall()
            for (msg_id, m_ts, m_data) in msgs:
                try: md = json.loads(m_data) if m_data else {}
                except Exception: md = {}
                role = (md.get('role') or '').lower()
                m_iso = common.full_ts({'time': m_ts}) if m_ts else (meta['ts'] or '')
                parts = conn.execute(
                    'SELECT time_created, data FROM part WHERE message_id=? ORDER BY time_created',
                    (msg_id,)).fetchall()
                for (p_ts, p_data) in parts:
                    try: part = json.loads(p_data) if p_data else {}
                    except Exception: continue
                    ptype = part.get('type', '')
                    p_iso = common.full_ts({'time': p_ts}) if p_ts else m_iso
                    if ptype == 'text':
                        txt = part.get('text') or ''
                        if not txt: continue
                        if role == 'user' and common.real_user(txt):
                            meta['turns'] += 1
                            if not meta['first_ucmd']: meta['first_ucmd'] = txt[:160]
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=p_iso,
                                            kind='ucmd', text=common.mask(txt[:600])))
                        elif role == 'assistant':
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=p_iso,
                                            kind='atext', text=common.mask(txt[:600])))
                    elif ptype == 'reasoning':
                        txt = part.get('text') or ''
                        if txt:
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=p_iso,
                                            kind='think', text=common.mask(txt[:600])))
                    elif ptype == 'tool':
                        nm = part.get('tool') or '?'
                        meta['ntools'] += 1
                        ev = dict(sid=sid, src='opencode', ws=meta['ws'], ts=p_iso, kind='tool', name=str(nm))
                        st = part.get('state') or {}
                        inp = st.get('input') or {}
                        if isinstance(inp, dict):
                            if inp.get('command'): ev['cmd'] = common.mask(str(inp['command'])[:200])
                            if inp.get('filePath') or inp.get('path'): ev['path'] = str(inp.get('filePath') or inp.get('path'))
                        evs.append(ev)
                        out = st.get('output')
                        status = st.get('status', '')
                        if out is not None or status:
                            ok = status != 'error' if status else True
                            if not ok: meta['nerrs'] += 1
                            evs.append(dict(sid=sid, src='opencode', ws=meta['ws'], ts=p_iso,
                                kind='result', ok=ok, text=common.mask(str(out)[:200])))
        except Exception as e:
            log.warning('opencode parse failed for %s/%s: %s', f, sess_id, e)
            return None, [], []
        finally:
            conn.close()
        return meta, evs, tokens
