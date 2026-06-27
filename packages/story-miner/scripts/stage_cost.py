"""阶段成本画像：用关联结果算 design/build/verify 各阶段的平均 turns/tools/nerrs/会话数。

阶段边界：用 story 的 ts_design/ts_build/ts_verify（阶段 gate 文件 mtime）切。
归因：session.ts（仅日期粒度）落在某阶段 [start_date, end_date] 区间内 -> 计入该阶段。
退路：session.ts 只有日期，若同一 story 的两个阶段边界落在同一天 -> 无法可靠切，
      退化为按 story 整体汇总（归到该 story 的最远阶段 stage）。

输出：scripts/out/stage_cost.md
"""
import os, sys, sqlite3, datetime, collections

# 允许 `python scripts/stage_cost.py` 直接跑（不加 PYTHONPATH）
_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)
from miner import config  # noqa: E402

DB = config.DB_PATH
OUT_DIR = os.path.join(_PROJ, 'scripts', 'out')
OUT_MD = os.path.join(OUT_DIR, 'stage_cost.md')

STAGE_ORDER = ['design', 'build', 'verify']


def _date(ts):
    if not ts:
        return None
    try:
        return datetime.date.fromisoformat(ts[:10])
    except ValueError:
        return None


def _stage_windows(story):
    """根据 story 的 ts_design/ts_build/ts_verify，返回 [(stage, start_date, end_date), ...]
    可靠区间。只保留能被日期级 session 切分的阶段（边界跨不同日期）。
    约定：阶段区间 = [该阶段 ts 日期, 下一阶段 ts 日期]。首个阶段起点用 first_ts 日期。
    """
    pts = []  # [(date, stage)]
    fdate = _date(story['first_ts'])
    for st in STAGE_ORDER:
        d = _date(story.get('ts_' + st))
        if d:
            pts.append((d, st))
    if not pts:
        return []
    pts.sort(key=lambda x: x[0])
    wins = []
    for i, (d, st) in enumerate(pts):
        start = fdate if i == 0 else pts[i - 1][0]
        end = d
        # 仅当区间跨天（start < end）才认为可切；同日无法用日期级 session 区分
        if start and end and start < end:
            wins.append((st, start, end))
    return wins


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')

    stories = {
        r[0]: {
            'story_id': r[0], 'title': r[1], 'stage': r[2],
            'first_ts': r[3], 'last_ts': r[4],
            'ts_design': r[5], 'ts_build': r[6], 'ts_verify': r[7],
        }
        for r in conn.execute(
            'SELECT story_id,title,stage,first_ts,last_ts,ts_design,ts_build,ts_verify '
            'FROM stories'
        )
    }
    sessions = conn.execute(
        'SELECT sid,story_id,ts,turns,ntools,nerrs FROM sessions '
        'WHERE story_id IS NOT NULL AND ts IS NOT NULL'
    ).fetchall()
    conn.close()

    # 归因：每 session -> stage（若可切）否则 -> story.stage（整体）
    # bucket[stage] = list of (turns, ntools, nerrs)
    stage_bucket = collections.defaultdict(list)
    per_story_rows = []  # (story_id, mode, stage_counts_str, n_sessions)
    n_staged = 0  # 成功切阶段的 story 数
    n_whole = 0   # 退化为整体汇总的 story 数

    # 先按 story 分组 session
    by_story = collections.defaultdict(list)
    for sid, story_id, ts, turns, tools, errs in sessions:
        by_story[story_id].append((ts, turns or 0, tools or 0, errs or 0))

    for story_id, sess_list in by_story.items():
        st = stories.get(story_id)
        if not st:
            continue
        wins = _stage_windows(st)
        cur_stage = st['stage'] or 'design'
        counts = collections.Counter()
        if wins:
            n_staged += 1
            for ts, turns, tools, errs in sess_list:
                sd = _date(ts)
                placed = None
                for sstage, sstart, send in wins:
                    if sstart <= sd <= send:
                        placed = sstage
                        break
                if placed is None:
                    # 落在最后阶段之后或窗口外 -> 归最远阶段（cur_stage）
                    placed = cur_stage
                stage_bucket[placed].append((turns, tools, errs))
                counts[placed] += 1
        else:
            n_whole += 1
            for ts, turns, tools, errs in sess_list:
                stage_bucket[cur_stage].append((turns, tools, errs))
                counts[cur_stage] += 1
        per_story_rows.append(
            (story_id, 'staged' if wins else 'whole', cur_stage,
             ', '.join(f'{k}:{v}' for k, v in sorted(counts.items())) or '-',
             len(sess_list)))

    # 汇总
    def stats(xs, idx):
        return round(sum(x[idx] for x in xs) / len(xs), 1) if xs else 0

    total = len(sessions)
    out = []
    out.append('# 阶段成本画像（session × story 关联）\n')
    out.append(f'> 数据源：transcripts.db（关联覆盖率见末尾）。阶段边界用 story 的阶段 gate 文件 mtime 切；')
    out.append(f'> session.ts 仅日期粒度，同日阶段无法切 -> 退化为按 story 整体汇总（归到该 story 最远阶段）。\n')
    out.append(f'> 关联 session 数：**{total}**；可切阶段 story：**{n_staged}**，整体汇总 story：**{n_whole}**。\n')

    out.append('\n## 各阶段平均成本\n')
    out.append('| 阶段 | 会话数 | 平均 turns | 平均 tools | 平均 errs |')
    out.append('|---|---|---|---|---|')
    for st in STAGE_ORDER:
        xs = stage_bucket.get(st, [])
        if not xs:
            out.append(f'| {st} | 0 | - | - | - |')
            continue
        out.append(f'| {st} | {len(xs)} | {stats(xs, 0)} | {stats(xs, 1)} | {stats(xs, 2)} |')

    # 全局均值对比基线
    if sessions:
        all_turns = [s[3] or 0 for s in sessions]
        all_tools = [s[4] or 0 for s in sessions]
        all_errs = [s[5] or 0 for s in sessions]
        out.append('\n## 参考：全部关联 session 整体均值\n')
        out.append(f'- 平均 turns: **{round(sum(all_turns)/len(all_turns),1)}**')
        out.append(f'- 平均 tools: **{round(sum(all_tools)/len(all_tools),1)}**')
        out.append(f'- 平均 errs:  **{round(sum(all_errs)/len(all_errs),1)}**')

    out.append('\n## 每个 story 的阶段归属\n')
    out.append('| story_id | 模式 | 最远阶段 | 各阶段会话数 | 总会话 |')
    out.append('|---|---|---|---|---|')
    for story_id, mode, cur_stage, cnts, n in sorted(per_story_rows, key=lambda x: -x[4]):
        st = stories.get(story_id, {})
        title = (st.get('title') or '')[:24]
        out.append(f'| {story_id} | {mode} | {cur_stage} | {cnts} | {n} |'
                   + (f'  <br><sub>{title}</sub>' if title else ''))

    # 关联覆盖率
    conn2 = sqlite3.connect(DB, timeout=30)
    total_sessions = conn2.execute('SELECT count(*) FROM sessions').fetchone()[0]
    linked = conn2.execute('SELECT count(*) FROM sessions WHERE story_id IS NOT NULL').fetchone()[0]
    n_stories = conn2.execute('SELECT count(*) FROM stories').fetchone()[0]
    conn2.close()
    out.append('\n## 关联覆盖率\n')
    out.append(f'- 挂上 story_id 的 session：**{linked}/{total_sessions} ({100*linked/total_sessions:.1f}%)**')
    out.append(f'- stories 表行数：**{n_stories}**')
    out.append(f'- 未挂上的 session 多为与具体 story 无明确信号（无 ID 提及 / 无 feature 分支匹配 / '
               f'不在唯一时间窗）的日常维护、排查类会话。')

    with open(OUT_MD, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out))

    # 控制台摘要（避免中文 mojibake，用文件为主）
    print(f"stage_cost -> {OUT_MD}")
    print(f"linked {linked}/{total_sessions} sessions ({100*linked/total_sessions:.1f}%), {n_stories} stories")
    print("stage profile (sessions / avg turns / avg tools / avg errs):")
    for st in STAGE_ORDER:
        xs = stage_bucket.get(st, [])
        if xs:
            print(f"  {st:7} n={len(xs):3}  turns={stats(xs,0):6}  tools={stats(xs,1):7}  errs={stats(xs,2):5}")
        else:
            print(f"  {st:7} n=0")


if __name__ == '__main__':
    main()
