"""持久化层：遍历 REGISTRY 中所有 adapter -> SQLite，统一 schema + FTS5 + 增量导入。
本文件不感知任何具体端；新增端只需在 miner/adapters/ 加 adapter 文件。"""
import argparse, sqlite3, os, time
from . import REGISTRY, config

DB = config.DB_PATH
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  path TEXT PRIMARY KEY, src TEXT, sid TEXT, mtime TEXT, size INT, n_events INT, ingested_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY, src TEXT, ws TEXT, ts TEXT, title TEXT,
  turns INT, ntools INT, nerrs INT, cwd TEXT, branch TEXT, first_ucmd TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, src TEXT, ws TEXT, ts TEXT, kind TEXT,
  name TEXT, cmd TEXT, code TEXT, ok INT, text TEXT, path TEXT);
CREATE INDEX IF NOT EXISTS idx_e_sid ON events(sid);
CREATE INDEX IF NOT EXISTS idx_e_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_e_name ON events(name);
CREATE INDEX IF NOT EXISTS idx_s_ws ON sessions(ws);
CREATE INDEX IF NOT EXISTS idx_s_ts ON sessions(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(text, code, cmd, content='events', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid,text,code,cmd) VALUES (new.id,new.text,new.code,new.cmd); END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts,rowid,text,code,cmd) VALUES('delete',old.id,old.text,old.code,old.cmd); END;
CREATE TABLE IF NOT EXISTS token_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, src TEXT, ts TEXT, model TEXT,
  input_tokens INT, output_tokens INT,
  cache_read_tokens INT, cache_creation_tokens INT, reasoning_tokens INT);
CREATE INDEX IF NOT EXISTS idx_tu_sid ON token_usage(sid);
"""

def discover():
    """yield (path, src, adapter, sid) —— 来自所有注册 adapter。"""
    for ad in REGISTRY:
        for path, sid in ad.discover():
            yield path, ad.name, ad, sid


def init_db(db_path=None):
    """Create the miner schema at ``db_path`` (defaults to config.DB_PATH).

    Safe to call repeatedly; uses CREATE TABLE IF NOT EXISTS.
    """
    path = db_path or DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _since_threshold(days: float) -> float | None:
    """Return mtime threshold for --since; None means no filtering."""
    if days <= 0:
        return None
    return time.time() - days * 86400


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest transcript files into transcripts.db")
    parser.add_argument(
        "--since-days", "-s", type=float, default=0,
        help="Only discover files modified within N days (0 = all)",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Override SQLite output path (defaults to config.db_path)",
    )
    args = parser.parse_args(argv)

    db_path = args.db or DB
    since = _since_threshold(args.since_days)

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path); conn.executescript(SCHEMA)
    print("registered adapters:", [(a.name, a.label) for a in REGISTRY])
    print(f"since filter: {args.since_days} day(s)" if since else "since filter: none (full scan)")

    known = {r[0]:(r[1],r[2]) for r in conn.execute('SELECT path,mtime,size FROM sources')}
    disk = {}
    for p,_,_,_ in discover():
        if not os.path.exists(p):
            continue
        if since and os.path.getmtime(p) < since:
            continue
        disk[p] = (str(int(os.path.getmtime(p))), os.path.getsize(p))

    # For incremental mode, also re-ingest known sources whose mtime/size changed,
    # even if they fall outside the window, so edits don't get stale.
    to_up = [p for p in disk if known.get(p) != (disk[p][0], disk[p][1])]
    # Deletion cleanup only in full mode; incremental should not purge old sources.
    to_del = [p for p in known if p not in disk] if not since else []

    t0 = time.time(); n_ev = 0; n_sess = 0
    for path, src, ad, sid in discover():
        if path not in disk:  # skipped by since filter
            continue
        if path not in to_up:
            continue
        conn.execute('DELETE FROM events WHERE sid=?', (sid,))
        conn.execute('DELETE FROM token_usage WHERE sid=?', (sid,))
        conn.execute('DELETE FROM sessions WHERE sid=?', (sid,))
        conn.execute('DELETE FROM sources WHERE path=?', (path,))
        meta, evs, tokens = ad.parse(path, sid)
        if meta is None or (meta['turns'] == 0 and meta['ntools'] == 0): continue
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (meta['sid'], meta['src'], meta['ws'], meta['ts'], meta['title'], meta['turns'],
             meta['ntools'], meta['nerrs'], meta['cwd'], meta['branch'], meta['first_ucmd'], path))
        rows = [(e.get('sid'), e.get('src'), e.get('ws'), e.get('ts'), e.get('kind'), e.get('name'),
                 e.get('cmd'), e.get('code'), e.get('ok'), e.get('text'), e.get('path')) for e in evs]
        conn.executemany('INSERT INTO events(sid,src,ws,ts,kind,name,cmd,code,ok,text,path) VALUES(?,?,?,?,?,?,?,?,?,?,?)', rows)
        if tokens:
            trows = [(t.get('sid'), t.get('src'), t.get('ts'), t.get('model'),
                      t.get('input_tokens'), t.get('output_tokens'),
                      t.get('cache_read_tokens'), t.get('cache_creation_tokens'),
                      t.get('reasoning_tokens')) for t in tokens]
            conn.executemany(
                'INSERT INTO token_usage(sid,src,ts,model,input_tokens,output_tokens,'
                'cache_read_tokens,cache_creation_tokens,reasoning_tokens) '
                'VALUES(?,?,?,?,?,?,?,?,?)', trows)
        conn.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?)',
            (path, src, sid, disk[path][0], disk[path][1], len(evs), time.strftime('%Y-%m-%dT%H:%M:%S')))
        n_ev += len(evs); n_sess += 1
    for p in to_del:
        for sid, in conn.execute('SELECT sid FROM sources WHERE path=?', (p,)):
            conn.execute('DELETE FROM events WHERE sid=?', (sid,))
            conn.execute('DELETE FROM token_usage WHERE sid=?', (sid,))
            conn.execute('DELETE FROM sessions WHERE sid=?', (sid,))
        conn.execute('DELETE FROM sources WHERE path=?', (p,))
    conn.commit()
    print(f"ingest done in {time.time()-t0:.1f}s")
    print(f"  updated sessions: {n_sess}, new events: {n_ev}, removed sources: {len(to_del)}")
    print("  sessions by src:", dict(conn.execute('SELECT src,Count(*) FROM sessions GROUP BY src').fetchall()))
    print("  total events:", conn.execute('SELECT Count(*) FROM events').fetchone()[0])
    print("  total sources:", conn.execute('SELECT Count(*) FROM sources').fetchone()[0])
    print("  db size: %.1f MB" % (os.path.getsize(db_path)/1048576))

if __name__ == '__main__':
    main()
