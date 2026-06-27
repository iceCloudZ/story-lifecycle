"""关联 session → story：回填 sessions.story_id（最多挂一个最匹配的 story，挂不上留 NULL）。

关联键（评分制，取最高分 story 且达到阈值才挂）：
  - workspace 匹配（基本门槛）：basename(story.workspace) == session.ws
  - ID 提及（最强 +100）：session.title/first_ucmd 含 story_id（含 tapd 数字尾）
  - branch 匹配（强 +30）：story.branch 非空且 == session.branch（feature 分支是 story 专属）
  - 时间邻近（弱 -1..+5）：session.ts 落在 [first_ts, last_ts] 窗内 +5；否则按距离扣分
                        （.story mtime 窗窄，只做弱信号，不当硬约束）

阈值：score >= 30 才挂（即至少要有 ID 提或 branch 命中；纯 workspace+时间不够）。
"""
import os, sqlite3, re, datetime
from . import config, story_ingest

DB = config.DB_PATH


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
    # 数字尾：tapd-1144381896001065458 -> 1065458；tapd-bug_...1001121 -> 1001121
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


def _score(sess, story, sid_tokens):
    """sess(story_id 待定) 与 story 行的匹配评分。返回 (score, reason) 或 None（不匹配）。"""
    # 基本门槛：workspace
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

    # 2) branch 匹配（强）—— 仅当 story.branch 非空
    sbranch = story['branch']
    if sbranch:
        if sess.get('branch') and sess['branch'] == sbranch:
            score += 30
            reasons.append('branch-match')
        elif sess.get('branch') and sess['branch'] not in (None, '', 'HEAD', 'master'):
            # story 指定了分支但 session 在别的 feature 分支 -> 负信号
            score -= 10
            reasons.append('branch-mismatch')

    # 3) 时间邻近（弱）
    sdate = _to_date(sess['ts'])
    fdate = _to_date(story['first_ts'])
    ldate = _to_date(story['last_ts'])
    if sdate and fdate and ldate:
        if fdate <= sdate <= ldate:
            score += 5
            reasons.append('in-window')
        else:
            # 距离最近窗口端点的天数，越远扣越多（封顶 -15）
            dist = min(abs((sdate - fdate).days), abs((sdate - ldate).days))
            score -= min(dist, 15)
            reasons.append(f'out-window-d{dist}')
    elif sdate and fdate:
        dist = abs((sdate - fdate).days)
        score -= min(dist, 15)

    return score, '+'.join(reasons) if reasons else 'workspace-only'


def link():
    conn = _connect()
    conn.execute('PRAGMA journal_mode=WAL')
    # ALTER ADD COLUMN（幂等）
    cols = [r[1] for r in conn.execute('PRAGMA table_info(sessions)')]
    if 'story_id' not in cols:
        conn.execute('ALTER TABLE sessions ADD COLUMN story_id TEXT')
    conn.execute('UPDATE sessions SET story_id = NULL')

    stories = [
        {
            'story_id': r[0], 'workspace': r[1], 'branch': r[2],
            'first_ts': r[3], 'last_ts': r[4],
        }
        for r in conn.execute(
            'SELECT story_id,workspace,branch,first_ts,last_ts FROM stories '
            'WHERE first_ts IS NOT NULL'
        )
    ]
    sid_token_map = {s['story_id']: _id_tokens(s['story_id']) for s in stories}

    sessions = conn.execute(
        'SELECT sid,ws,ts,title,first_ucmd,branch FROM sessions'
    ).fetchall()

    THRESHOLD = 30  # 至少需 ID 提及或 branch 命中（workspace+时间不够挂）
    UNIQ_WIN_THRESHOLD = 5  # 时间窗内且该 session 唯一属于一个 story 时，弱阈值
    n_linked = 0
    per_story = {}
    for sid, ws, ts, title, first_ucmd, branch in sessions:
        sess = {'ws': ws, 'ts': ts, 'title': title, 'first_ucmd': first_ucmd, 'branch': branch}
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

        linked = False
        if best_sc >= THRESHOLD:
            linked = True
        elif best_sc >= UNIQ_WIN_THRESHOLD and 'in-window' in best_reason:
            # 次级路径：workspace 匹配 + 落在窗口内（5分）= 5，需为唯一候选
            in_win = [s for s, r, _ in scored if 'in-window' in r]
            if len(in_win) == 1:
                linked = True
        if linked:
            conn.execute('UPDATE sessions SET story_id=? WHERE sid=?', (best_sid, sid))
            n_linked += 1
            per_story.setdefault(best_sid, [0, best_reason])
            per_story[best_sid][0] += 1
    conn.commit()

    total = len(sessions)
    print(f"link done: {n_linked}/{total} sessions linked "
          f"({100*n_linked/total:.1f}%)")
    for sid, (cnt, reason) in sorted(per_story.items(), key=lambda x: -x[1][0]):
        print(f"  {sid:45} {cnt:3} sessions  (sample: {reason})")
    conn.close()


def main():
    link()


if __name__ == '__main__':
    main()
