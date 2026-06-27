"""Story 维度桥接层：遍历 config workspaces 的 .story/，提取 story 维度 → stories 表。

.story/ 文件式状态（story-lifecycle）：
  <ws>/.story/context/<id>/   进行中的 story
      prompt_<stage>.md        阶段任务书（含 Key / 标题）
      plan_<stage>.md          任务书（含 标题/分支线索）
      review_<stage>.md        阶段评审
      gates/<stage>-review-gate.md   阶段 gate 决策
      done/<stage>.json        阶段完成产物 {spec_path, complexity, summary, files_changed}
      archive/...              归档的旧轮次产物
  <ws>/.story/done/<id>/       完成
      design.json              {spec_path, complexity, summary, affected_repos:[{path,name,reason}]}
  <ws>/.story/knowledge/       项目知识库（非 story，跳过）

最佳努力解析：格式不统一，解析失败跳过单个 story，不崩溃。
状态语义：active=在 context / done=在 done。
阶段时间戳用对应阶段文件 mtime。
"""
import os, json, sqlite3, datetime, re, glob
from . import config

DB = config.DB_PATH

# 阶段名归一：implement ≈ build；verify 通过 gate 体现。
STAGE_ALIASES = {'implement': 'build'}
STAGE_ORDER = ['design', 'build', 'verify']

SCHEMA_STORIES = """
CREATE TABLE IF NOT EXISTS stories(
  story_id   TEXT PRIMARY KEY,
  workspace  TEXT,
  title      TEXT,
  status     TEXT,            -- active | done
  stage      TEXT,            -- design | build | verify (当前/最远阶段)
  spec_path  TEXT,
  complexity TEXT,
  branch     TEXT,            -- 从 affected_repos / prompt 提取的分支线索（可空）
  ts_design  TEXT,            -- 阶段 gate 文件 mtime（ISO）
  ts_build   TEXT,
  ts_verify  TEXT,
  first_ts   TEXT,            -- 最早阶段文件 mtime
  last_ts    TEXT,            -- 最晚阶段文件 mtime
  dir_path   TEXT             -- .story 子目录全路径，便于复查
);
"""


def _iso(mtime):
    return datetime.datetime.fromtimestamp(mtime).replace(microsecond=0).isoformat()


def _read_text(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return ''


STAGE_WORDS = {'design', 'build', 'implement', 'verify', '任务书', '任务', '阶段'}


def _extract_title(text, sid):
    """从 prompt/plan markdown 推断标题。返回 None 表示未识别。"""
    # prompt_*.md:  '### Story 信息' 段里 '标题: xxx'（最权威）
    m = re.search(r'标题[:：]\s*(.+)', text)
    if m:
        t = m.group(1).strip().strip('`*')
        if t and t.lower() not in STAGE_WORDS:
            return t
    # plan_*.md 标题行（多种写法，均为 H2 ## 开头，排除 H1 '# 任务书: design' 纯阶段词）：
    #   '## 任务书：<id> <title>'            (1065518)
    #   '## 任务：<id> <title> - 副标题'     (1064837)
    #   '## 任务书：<title>'                 (1064993 用 '## 任务书：1064993 ...')
    for m in re.finditer(r'^##\s*任务[书]?[:：]\s*(.+)', text, re.M):
        line = m.group(1).strip()
        # 去掉前导 STORY-<id> 或 <id>
        cleaned = re.sub(r'^(?:STORY-)?' + re.escape(str(sid)) + r'[\s\-：:]*', '', line).strip()
        cleaned = cleaned.strip('`*- ')
        if cleaned and cleaned.lower() not in STAGE_WORDS and len(cleaned) >= 2:
            return cleaned
    return None


def _extract_branch(text):
    """从文本里提取分支名线索（如 feature/zzh/contact_verify_0610）。"""
    # 常见分支模式
    for m in re.finditer(r'(feature|fix|hotfix|release)/[\w./-]{4,}', text):
        return m.group(0)
    return None


def _scan_stages(story_dir):
    """扫描 story_dir，返回 {stage: {'mtime':iso, 'files':[...]}} 与 (first_ts,last_ts)。
    阶段信号文件：
      gates/<stage>-review-gate.md   -> 阶段到达/决策（权威）
      done/<stage>.json              -> 阶段完成（权威，优先用其 mtime）
      prompt_<stage>.md / plan_<stage>.md / review_<stage>.md -> 阶段产物（弱信号）
    """
    stages = {}
    mtimes = []  # 全部文件 mtime 用于 first/last
    if not os.path.isdir(story_dir):
        return stages, (None, None)

    def norm(stage):
        return STAGE_ALIASES.get(stage, stage)

    def bump(stage, mt, fname):
        stage = norm(stage)
        if stage not in STAGE_ORDER:
            return
        rec = stages.setdefault(stage, {'mtime': None, 'files': []})
        rec['files'].append(fname)
        if mt and (rec['mtime'] is None or mt > rec['mtime']):
            rec['mtime'] = mt

    for root, _dirs, files in os.walk(story_dir):
        for f in files:
            full = os.path.join(root, f)
            try:
                mt = _iso(os.path.getmtime(full))
            except OSError:
                continue
            mtimes.append(mt)
            rel = os.path.relpath(full, story_dir).replace('\\', '/')
            # gates/<stage>-review-gate.md
            gm = re.match(r'gates/([\w-]+)-review-gate\.md$', rel)
            if gm:
                bump(gm.group(1), mt, rel)
                continue
            # done/<stage>.json
            dm = re.match(r'done/([\w-]+)\.json$', rel)
            if dm:
                bump(dm.group(1), mt, rel)
                continue
            # prompt_<stage>.md / plan_<stage>.md / review_<stage>.md / repair_<stage>_round*.md
            pm = re.match(r'(?:prompt|plan|review|repair)_([\w-]+?)(?:_round\d+)?\.md$', rel)
            if pm:
                bump(pm.group(1), mt, rel)
                continue
    first_ts = min(mtimes) if mtimes else None
    last_ts = max(mtimes) if mtimes else None
    return stages, (first_ts, last_ts)


def _current_stage(stages):
    """当前阶段 = 已到达的最后一个阶段（按 STAGE_ORDER）。"""
    reached = [s for s in STAGE_ORDER if s in stages]
    return reached[-1] if reached else None


def _find_design_json(story_dir):
    """找 design.json：优先 done/design.json（顶层 or context 内 done/）。"""
    cand = [
        os.path.join(story_dir, 'done', 'design.json'),
        os.path.join(story_dir, 'design.json'),
    ]
    for p in cand:
        if os.path.isfile(p):
            return p
    return None


def _load_json(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def parse_story(story_id, story_dir, extra_dirs, workspace, status):
    """解析单个 story 目录 -> dict 行（失败返回 None）。
    extra_dirs: 同 story 的 done 目录（含最终 design.json），补充读入。
    """
    try:
        # 扫描所有相关目录的阶段文件（主目录 + done 目录合并）
        stages, (first_ts, last_ts) = _scan_stages(story_dir)
        all_dirs = [story_dir] + list(extra_dirs)
        for ed in extra_dirs:
            st2, rng2 = _scan_stages(ed)
            for stg, rec in st2.items():
                cur = stages.setdefault(stg, {'mtime': None, 'files': []})
                cur['files'].extend(rec['files'])
                if rec['mtime'] and (cur['mtime'] is None or rec['mtime'] > cur['mtime']):
                    cur['mtime'] = rec['mtime']
            if rng2[0] and (first_ts is None or rng2[0] < first_ts):
                first_ts = rng2[0]
            if rng2[1] and (last_ts is None or rng2[1] > last_ts):
                last_ts = rng2[1]

        title = None
        branch = None
        spec_path = None
        complexity = None

        # 1) design.json：优先 done 目录里的（最终产物），其次 context 内
        dj = None
        for d in all_dirs:
            dj = _find_design_json(d)
            if dj:
                break
        design = _load_json(dj) if dj else None
        if isinstance(design, dict):
            spec_path = design.get('spec_path')
            complexity = design.get('complexity')
            if not title and design.get('summary'):
                title = design['summary'].split('。')[0].split('\n')[0][:120]
            repos = design.get('affected_repos') or []
            if isinstance(repos, list):
                for r in repos:
                    if isinstance(r, dict) and r.get('name'):
                        # affected_repos 给的是仓库名/路径，不是分支；仅作弱线索
                        pass

        # 2) prompt/plan markdown —— 标题 + 分支线索
        #    优先取最新阶段的 prompt/plan
        md_files = sorted(
            glob.glob(os.path.join(story_dir, '**', '*_*.md'), recursive=True),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
            reverse=True,
        )
        for mf in md_files:
            txt = _read_text(mf)
            if not txt:
                continue
            if title is None:
                title = _extract_title(txt, story_id)
            if branch is None:
                branch = _extract_branch(txt)
            if title is not None and branch is not None:
                break

        # 3) spec_path 仍空 -> 尝试读 done/<stage>.json 的 spec_path
        if not spec_path:
            for st in ('build', 'design', 'verify'):
                p = os.path.join(story_dir, 'done', f'{st}.json')
                d = _load_json(p)
                if isinstance(d, dict) and d.get('spec_path'):
                    spec_path = d['spec_path']
                    break

        stage = _current_stage(stages)
        # 无阶段 gate 文件但有 design.json -> 退化为 design 阶段（done story 常见）
        if stage is None and isinstance(design, dict):
            stage = 'design'
            if not stages.get('design', {}).get('mtime'):
                # 用 design.json 自身 mtime 兜底
                try:
                    stages.setdefault('design', {'mtime': None, 'files': []})['mtime'] = _iso(
                        os.path.getmtime(dj)) if dj else last_ts
                except OSError:
                    pass
        ts_design = stages.get('design', {}).get('mtime')
        ts_build = stages.get('build', {}).get('mtime')
        ts_verify = stages.get('verify', {}).get('mtime')

        return {
            'story_id': story_id,
            'workspace': workspace,
            'title': title,
            'status': status,
            'stage': stage,
            'spec_path': spec_path,
            'complexity': complexity,
            'branch': branch,
            'ts_design': ts_design,
            'ts_build': ts_build,
            'ts_verify': ts_verify,
            'first_ts': first_ts,
            'last_ts': last_ts,
            'dir_path': story_dir,
        }
    except Exception:
        # 最佳努力：单 story 解析失败不崩溃
        return None


def discover_stories():
    """遍历所有 workspace 的 .story/{context,done}，按 story_id 合并目录。

    同一 story 可能同时在 context/（活跃、含丰富阶段产物）与 done/（完成标记 +
    最终 design.json）。合并策略：context 优先作为主目录（status=active），done 的
    design.json 作为补充读入；仅有 done/ 时 status=done。
    返回 dict: story_id -> (primary_dir, extra_dirs:list, workspace, status)
    """
    grouped = {}  # sid -> {'context': dir, 'done': dir, 'ws': ws}
    for ws in config.WORKSPACES:
        story_root = os.path.join(ws, '.story')
        for sub in ('context', 'done'):
            base = os.path.join(story_root, sub)
            if not os.path.isdir(base):
                continue
            for sid in os.listdir(base):
                d = os.path.join(base, sid)
                if os.path.isdir(d) and sid != 'knowledge':
                    grouped.setdefault(sid, {'context': None, 'done': None, 'ws': ws})[sub] = d
    for sid, g in grouped.items():
        if g['context']:
            primary = g['context']
            status = 'active'
        else:
            primary = g['done']
            status = 'done'
        extras = [d for d in (g['done'],) if d and d != primary]
        yield sid, primary, extras, g['ws'], status


def _connect():
    """带 busy_timeout + 重试的连接（并发只读 subagent 偶发锁住 db，自动重试）。"""
    import time
    for attempt in range(60):
        try:
            conn = sqlite3.connect(DB, timeout=30)
            conn.execute('PRAGMA busy_timeout=30000')
            return conn
        except sqlite3.OperationalError:
            time.sleep(1)
    raise RuntimeError('db locked after retries')


def main():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = _connect()
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA_STORIES)
    conn.execute('DELETE FROM stories')
    rows = []
    issues = []
    for sid, primary, extras, ws, status in discover_stories():
        row = parse_story(sid, primary, extras, ws, status)
        if row is None:
            issues.append(sid)
            continue
        rows.append(row)
    conn.executemany(
        'INSERT INTO stories(story_id,workspace,title,status,stage,spec_path,complexity,'
        'branch,ts_design,ts_build,ts_verify,first_ts,last_ts,dir_path) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(r['story_id'], r['workspace'], r['title'], r['status'], r['stage'],
          r['spec_path'], r['complexity'], r['branch'], r['ts_design'], r['ts_build'],
          r['ts_verify'], r['first_ts'], r['last_ts'], r['dir_path']) for r in rows],
    )
    conn.commit()
    print(f"stories ingested: {len(rows)}")
    for r in rows:
        print(f"  [{r['status']:6}] {r['story_id']:42} stage={r['stage']} "
              f"complexity={r['complexity']} title={(r['title'] or '')[:40]!r}")
    if issues:
        print(f"  parse-failed (skipped): {issues}")
    print("  by workspace:", dict(conn.execute('SELECT workspace,count(*) FROM stories GROUP BY workspace')))
    print("  by status:   ", dict(conn.execute('SELECT status,count(*) FROM stories GROUP BY status')))
    conn.close()


if __name__ == '__main__':
    main()
