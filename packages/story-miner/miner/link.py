"""关联 session → story：回填 sessions.story_id（最多挂一个最匹配的 story，挂不上留 NULL）。

关联分两阶段：
  1. Anchor 精确匹配（高置信）: story-lifecycle 在 inject_prompt 时写 anchors.jsonl，
     link 优先用 (cwd + ts 之后最近 session) 精确命中并回填 story_id。
  2. 启发式评分（低置信/兜底）: 对未命中 anchor 的 session，用 id-mention / branch-match 评分，
     阈值 30 才挂。不再使用纯时间窗兜底，避免宽窗误绑（如 1064837 挂 84 个 session）。

评分信号：
  - workspace 匹配（基本门槛）
  - ID 提及（最强 +100）：session.title/first_ucmd 含 story_id（含 tapd 数字尾）
  - branch 匹配（强 +30）
"""
import os, sqlite3, re, datetime
from . import config, story_ingest
from .anchors import iter_workspace_anchors
from .common import ws_of

DB = config.DB_PATH


THRESHOLD = 30            # 启发式阶段至少需 ID 提及或 branch 命中
ANCHOR_LOOKAHEAD_DAYS = 7  # anchor 后最多看 N 天，防止回填过远未来会话


def _connect():
    import time
    for _ in range(60):
        try:
            conn = sqlite3.connect(DB, timeout=30)
            conn.execute('PRAGMA busy_timeout=30000')
            return conn
        except sqlite3.OperationalError:
            time.sleep(1)
    raise RuntimeError('db locked after retries')


def _ws_basename(ws_path):
    return os.path.basename(ws_path.replace('\\', '/').rstrip('/'))


def _id_tokens(story_id):
    """从一个 story_id 提取所有可用于文本匹配的 token。"""
    toks = {story_id, story_id.lower()}
    for m in re.finditer(r'(\d{5,})', story_id):
        toks.add(m.group(1))
    return toks


def _to_date(ts):
    """'2026-06-22T...' / '2026-06-22' -> date；None 返回 None。"""
    if not ts:
        return None
    try:
        return datetime.date.fromisoformat(ts[:10])
    except ValueError:
        return None


def _to_datetime(ts):
    """Parse iso datetime; return None on failure."""
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None


def _link_anchors(conn, sessions):
    """Phase 1: high-confidence binding from story-lifecycle anchors.

    Returns dict sid -> (story_id, reason) for anchor-matched sessions.
    """
    # Build quick indexes
    by_sid = {s['sid']: s for s in sessions}

    linked = {}
    n_anchor_hits = 0
    for ws in config.WORKSPACES:
        for story_key, anchors in iter_workspace_anchors(ws):
            for anchor in anchors:
                anchor_ws = ws_of(anchor.get('cwd', ws))
                anchor_dt = _to_datetime(anchor.get('ts'))
                anchor_date = _to_date(anchor.get('ts'))
                anchor_cwd = anchor.get('cwd')
                if not anchor_date:
                    continue

                # candidate sessions: same ws, ts >= anchor_date, within lookahead
                best = None
                best_key = None
                for sid, sess in by_sid.items():
                    if sid in linked:
                        continue
                    if sess['ws'] != anchor_ws:
                        continue
                    sdate = _to_date(sess['ts'])
                    if not sdate or sdate < anchor_date:
                        continue
                    if (sdate - anchor_date).days > ANCHOR_LOOKAHEAD_DAYS:
                        continue
                    # cwd match is strong signal when available
                    if anchor_cwd and sess.get('cwd') and sess['cwd'].replace('\\', '/') != anchor_cwd.replace('\\', '/'):
                        continue
                    # pick session closest to anchor datetime (or earliest date)
                    score = (sdate - anchor_date).days
                    if anchor_dt and sess.get('ts'):
                        sdt = _to_datetime(sess['ts'])
                        if sdt and sdt >= anchor_dt:
                            score = (sdt - anchor_dt).total_seconds()
                    if best is None or score < best:
                        best = score
                        best_key = sid

                if best_key:
                    linked[best_key] = (story_key, 'anchor')
                    n_anchor_hits += 1

    return linked, n_anchor_hits


def _score(sess, story, sid_tokens):
    """启发式评分。返回 (score, reason) 或 None（workspace 不匹配）。"""
    if _ws_basename(story['workspace']) != sess['ws']:
        return None
    score = 0
    reasons = []

    # 1) ID 提及（最强）
    blob = ((sess.get('title') or '') + ' ' + (sess.get('first_ucmd') or '')).lower()
    for tok in sid_tokens:
        if tok and tok in blob:
            score += 100
            reasons.append('id-mention')
            break

    # 2) branch 匹配（强）
    sbranch = story['branch']
    if sbranch:
        if sess.get('branch') and sess['branch'] == sbranch:
            score += 30
            reasons.append('branch-match')
        elif sess.get('branch') and sess['branch'] not in (None, '', 'HEAD', 'master'):
            score -= 10
            reasons.append('branch-mismatch')

    return score, '+'.join(reasons) if reasons else 'workspace-only'


def _numeric_tail(s):
    m = re.search(r'(\d{5,})$', s or '')
    return m.group(1) if m else None


def _build_story_map(conn):
    """Build story metadata from DB stories table + anchor files.

    Anchor-only stories (no .story/ dir) are included so that ID-mention
    heuristics can still bind sessions that reference them.
    """
    by_id = {}
    for r in conn.execute(
        'SELECT story_id,workspace,branch,first_ts,last_ts FROM stories '
        'WHERE first_ts IS NOT NULL'
    ):
        story = {'story_id': r[0], 'workspace': r[1], 'branch': r[2],
                 'first_ts': r[3], 'last_ts': r[4]}
        by_id[r[0]] = story

    for ws in config.WORKSPACES:
        for story_key, anchors in iter_workspace_anchors(ws):
            if not anchors:
                continue
            # normalize to existing story if numeric tail matches
            existing_key = None
            if story_key in by_id:
                existing_key = story_key
            else:
                tail = _numeric_tail(story_key)
                for sid, st in by_id.items():
                    if tail and tail == _numeric_tail(sid):
                        existing_key = sid
                        break
            if existing_key:
                story = by_id[existing_key]
            else:
                story = {'story_id': story_key, 'workspace': ws, 'branch': None,
                         'first_ts': None, 'last_ts': None}
                by_id[story_key] = story
            # widen ts window from anchors
            ts_list = [_to_datetime(a.get('ts')) for a in anchors]
            ts_list = [t for t in ts_list if t]
            if ts_list:
                first = min(ts_list).replace(microsecond=0).isoformat()
                last = max(ts_list).replace(microsecond=0).isoformat()
                if story.get('first_ts') is None or first < story['first_ts']:
                    story['first_ts'] = first
                if story.get('last_ts') is None or last > story['last_ts']:
                    story['last_ts'] = last

    return list(by_id.values())


def link():
    conn = _connect()
    conn.execute('PRAGMA journal_mode=WAL')
    # ALTER ADD COLUMN（幂等）
    cols = [r[1] for r in conn.execute('PRAGMA table_info(sessions)')]
    if 'story_id' not in cols:
        conn.execute('ALTER TABLE sessions ADD COLUMN story_id TEXT')
    conn.execute('UPDATE sessions SET story_id = NULL')

    stories = _build_story_map(conn)
    sid_token_map = {s['story_id']: _id_tokens(s['story_id']) for s in stories}

    # load sessions as dicts
    rows = conn.execute(
        'SELECT sid,ws,ts,title,first_ucmd,branch FROM sessions'
    ).fetchall()
    sessions = [
        {'sid': sid, 'ws': ws, 'ts': ts, 'title': title, 'first_ucmd': first_ucmd, 'branch': branch}
        for sid, ws, ts, title, first_ucmd, branch in rows
    ]

    # --- Phase 1: anchors (high confidence) ---
    anchor_linked, n_anchor_hits = _link_anchors(conn, sessions)
    for sid, (story_id, reason) in anchor_linked.items():
        conn.execute('UPDATE sessions SET story_id=? WHERE sid=?', (story_id, sid))

    # --- Phase 2: heuristic fallback (low confidence, stricter threshold) ---
    linked_sids = set(anchor_linked.keys())
    n_heuristic = 0
    per_story = {}
    for sess in sessions:
        sid = sess['sid']
        if sid in linked_sids:
            continue
        scored = []
        for st in stories:
            res = _score(sess, st, sid_token_map[st['story_id']])
            if res is None:
                continue
            scored.append((res[0], res[1], st['story_id']))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[0], x[2]))
        best_sc, best_reason, best_sid = scored[0]

        if best_sc >= THRESHOLD:
            conn.execute('UPDATE sessions SET story_id=? WHERE sid=?', (best_sid, sid))
            n_heuristic += 1
            per_story.setdefault(best_sid, [0, best_reason])
            per_story[best_sid][0] += 1

    conn.commit()

    total = len(sessions)
    n_linked = len(anchor_linked) + n_heuristic
    print(f"link done: {n_linked}/{total} sessions linked "
          f"({100*n_linked/total:.1f}%)")
    print(f"  anchor-linked (high-confidence): {n_anchor_hits}")
    print(f"  heuristic-linked (low-confidence): {n_heuristic}")
    for sid, (cnt, reason) in sorted(per_story.items(), key=lambda x: -x[1][0]):
        print(f"  {sid:45} {cnt:3} sessions  (sample: {reason})")
    conn.close()


def main():
    link()


if __name__ == '__main__':
    main()
