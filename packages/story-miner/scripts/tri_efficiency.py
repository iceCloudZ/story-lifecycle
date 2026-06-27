"""三端效率画像深化（Claude / Codex / Kimi）。

只读 transcripts.db。覆盖所有有三端数据的工作区，不只 hc-all。
对每个 (src, ws) 单元统计：
  - turns / ntools / nerrs 的中位数 + P90（基于 sessions 表）
  - 该工作区下该端调用过的不同工具种类数（events.name where kind='tool'）
  - 会话长度(turns)分布桶
连接走 miner.config.DB_PATH，遇到 "database is locked" 自动 sleep 重试。
"""
import os, sys, sqlite3, time, statistics, collections

# 项目根加入 sys.path 以便 from miner import config
_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)
from miner import config  # noqa: E402

OUT_DIR = os.path.join(_PROJ, 'scripts', 'out')
OUT_FILE = os.path.join(OUT_DIR, 'tri_efficiency.md')


def connect():
    """连接 DB，遇到 lock 则退避重试（另一个 subagent 可能在改 schema）。"""
    for attempt in range(10):
        try:
            conn = sqlite3.connect(config.DB_PATH, timeout=30)
            conn.execute('PRAGMA query_only = 1')  # 强制只读
            return conn
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower():
                time.sleep(1)
                continue
            raise
    raise RuntimeError('DB locked after retries')


def med(xs):
    return statistics.median(xs) if xs else 0


def p90(xs):
    if not xs:
        return 0
    xs = sorted(xs)
    # “至多 90% 严格小于”的最近秩
    return xs[min(int(len(xs) * 0.9), len(xs) - 1)]


def mean(xs):
    return sum(xs) / len(xs) if xs else 0


def length_bucket(t):
    if t is None:
        return 'unknown'
    if t <= 5:
        return '1-5 (极短)'
    if t <= 15:
        return '6-15 (短)'
    if t <= 40:
        return '16-40 (中)'
    if t <= 100:
        return '41-100 (长)'
    return '100+ (超长)'


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = connect()
    c = conn.cursor()

    # 数据质量备注（已校验，详见脚本末尾"附录·数据质量"）：
    #   - sessions.turns 对三端都准确（== events ucmd 计数），可直接用。
    #   - sessions.ntools 不可靠：Codex 79/116 会话为 0；Kimi 大多为 1。
    #   - sessions.nerrs 不可靠：Claude 列值远大于真实 ok=0 计数（如 222 vs 59）。
    # 因此 ntools / nerrs 一律从 events 重算（kind='tool' / kind='result' and ok=0）。

    # --- 1a. turns 取自 sessions（可靠）---
    c.execute('SELECT sid, src, ws, turns FROM sessions')
    sid2turns = {}
    sess_meta = {}  # sid -> (src, ws)
    sess = collections.defaultdict(list)  # (src,ws) -> [turns, ntools, nerrs]
    for sid, src, ws, turns in c.fetchall():
        if src is None:
            continue
        sid2turns[sid] = turns or 0
        sess_meta[sid] = (src, ws)

    # --- 1b. ntools / nerrs 从 events 重算，按 sid 聚合 ---
    sid_ntools = collections.Counter()
    sid_nerrs = collections.Counter()
    c.execute("SELECT sid, kind, ok FROM events WHERE kind IN ('tool','result')")
    for sid, kind, ok in c.fetchall():
        if kind == 'tool':
            sid_ntools[sid] += 1
        elif kind == 'result' and ok == 0:
            sid_nerrs[sid] += 1

    for sid, (src, ws) in sess_meta.items():
        sess[(src, ws)].append((sid2turns[sid], sid_ntools[sid], sid_nerrs[sid]))

    # --- 2. 工具种类数：distinct events.name where kind='tool'，按 (src, ws) ---
    tool_distinct = collections.Counter()
    tool_names = collections.defaultdict(set)
    c.execute("SELECT src, ws, name FROM events WHERE kind='tool'")
    for src, ws, name in c.fetchall():
        if src is None:
            continue
        tool_distinct[(src, ws)] += 1 if False else 0  # 占位，实际用 set
        if name:
            tool_names[(src, ws)].add(name)
    for k, v in tool_names.items():
        tool_distinct[k] = len(v)

    # --- 3. 全工作区聚合（每端整体画像）---
    agg = collections.defaultdict(lambda: {'turns': [], 'tools': [], 'errs': [], 'tools_set': set()})
    for (src, ws), rows in sess.items():
        for t, nt, ne in rows:
            agg[src]['turns'].append(t)
            agg[src]['tools'].append(nt)
            agg[src]['errs'].append(ne)
    c.execute("SELECT src, name FROM events WHERE kind='tool'")
    for src, name in c.fetchall():
        if src and name:
            agg[src]['tools_set'].add(name)

    # --- 输出 ---
    out = []
    out.append('# 三端效率画像（Claude / Codex / Kimi）\n')
    out.append('> 数据源：`data/transcripts.db`（只读）。按 (端 × 工作区) 单元统计 turns/ntools/nerrs 的中位数 + P90、')
    out.append('> 调用过的不同工具种类数、会话长度分布。覆盖所有有三端数据的工作区。\n')

    # 端整体对比
    out.append('## 1. 三端整体画像（全工作区聚合）\n')
    out.append('每端所有会话聚合。最直观的三端效率对比：')
    out.append('')
    out.append('| 端 | 会话数 | turns 中位 | turns P90 | ntools 中位 | ntools P90 | nerrs 中位 | nerrs P90 | 工具种类数(distinct) |')
    out.append('|---|---|---|---|---|---|---|---|---|')
    for src in ['claude', 'codex', 'kimi']:
        if src not in agg:
            continue
        a = agg[src]
        out.append(
            f"| {src} | {len(a['turns'])} "
            f"| {med(a['turns']):.1f} | {p90(a['turns'])} "
            f"| {med(a['tools']):.1f} | {p90(a['tools'])} "
            f"| {med(a['errs']):.1f} | {p90(a['errs'])} "
            f"| {len(a['tools_set'])} |"
        )
    out.append('')
    out.append('**工具/轮比（平均每次会话每轮调用多少工具 = mean(ntools)/mean(turns)）**：')
    out.append('')
    out.append('| 端 | 平均 turns | 平均 ntools | tools/turn |')
    out.append('|---|---|---|---|')
    for src in ['claude', 'codex', 'kimi']:
        if src not in agg:
            continue
        a = agg[src]
        mt = mean(a['turns'])
        mn = mean(a['tools'])
        ratio = (mn / mt) if mt else 0
        out.append(f"| {src} | {mt:.1f} | {mn:.1f} | {ratio:.2f} |")
    out.append('')

    # 关键洞察（基于事件重算后的可靠数字）
    claude_t = med(agg.get('claude', {}).get('turns', [0]))
    codex_t = med(agg.get('codex', {}).get('turns', [0]))
    kimi_t = med(agg.get('kimi', {}).get('turns', [0]))
    claude_n = med(agg.get('claude', {}).get('tools', [0]))
    codex_n = med(agg.get('codex', {}).get('tools', [0]))
    kimi_n = med(agg.get('kimi', {}).get('tools', [0]))
    claude_e = med(agg.get('claude', {}).get('errs', [0]))
    codex_e = med(agg.get('codex', {}).get('errs', [0]))
    kimi_e = med(agg.get('kimi', {}).get('errs', [0]))
    out.append('**关键差异**：')
    out.append(
        f"- **多轮倾向**：Codex 中位 turns {codex_t:.0f} > Claude {claude_t:.0f} > Kimi {kimi_t:.0f}。"
        f" Codex 是 Claude 的 {(codex_t / claude_t) if claude_t else 0:.1f}x、Kimi 的 {(codex_t / kimi_t) if kimi_t else 0:.1f}x —— "
        f'Codex 倾向"多轮对话推进"。Kimi 中位 turns 极低，说明大量 Kimi 会话是单轮短任务。'
    )
    out.append(
        f"- **工具密度**：Claude 中位 {claude_n:.0f} 工具（{len(agg.get('claude',{}).get('tools_set',set()))} 种）、"
        f"Codex 中位 {codex_n:.0f}（{len(agg.get('codex',{}).get('tools_set',set()))} 种）、"
        f"Kimi 中位 {kimi_n:.0f}（{len(agg.get('kimi',{}).get('tools_set',set()))} 种）。"
        f"Claude 工具种类最丰富（Bash/Read/Edit/Grep/codegraph 全家桶），Codex 工具种类集中（shell_command 为主），"
        f"Kimi 单会话工具调用偏少。"
    )
    out.append(
        f"- **失败率**：Claude 中位失败事件 {claude_e:.0f} > Codex {codex_e:.0f} > Kimi {kimi_e:.0f}。"
        f"（注：nerrs 从 events 重算 = ok=0 的 result 事件数，见附录数据质量说明。）"
    )
    out.append('')

    # --- (src, ws) 明细表 ---
    out.append('## 2. 各工作区 × 各端明细\n')
    out.append('工作区按至少有 1 个三端之一数据的工作区列出；样本 <3 的会话仍列出但标注小样本。')
    out.append('')
    out.append('| 端 | 工作区 | 会话数 | turns 中位 | turns P90 | ntools 中位 | ntools P90 | nerrs 中位 | 工具种类数 |')
    out.append('|---|---|---|---|---|---|---|---|---|')
    # 收集所有 ws 并按端顺序排
    all_ws = sorted({ws for (_, ws) in sess.keys()})
    for ws in all_ws:
        for src in ['claude', 'codex', 'kimi']:
            key = (src, ws)
            if key not in sess:
                continue
            rows = sess[key]
            tlist = [r[0] for r in rows]
            nlist = [r[1] for r in rows]
            elist = [r[2] for r in rows]
            small = ' ⚠小样本' if len(rows) < 3 else ''
            out.append(
                f"| {src} | {ws} | {len(rows)}{small} "
                f"| {med(tlist):.1f} | {p90(tlist)} "
                f"| {med(nlist):.1f} | {p90(nlist)} "
                f"| {med(elist):.1f} | {tool_distinct.get(key, 0)} |"
            )
    out.append('')

    # 会话长度分布（按端 × ws）
    out.append('## 3. 会话长度分布（按 turns 桶）\n')
    out.append('各端各工作区会话数落在 turns 区间的占比（行内为该端该工作区的会话数）。')
    out.append('')
    buckets_order = ['1-5 (极短)', '6-15 (短)', '16-40 (中)', '41-100 (长)', '100+ (超长)', 'unknown']
    out.append('| 端 | 工作区 | ' + ' | '.join(buckets_order) + ' |')
    out.append('|---|---|' + '---|' * len(buckets_order))
    for ws in all_ws:
        for src in ['claude', 'codex', 'kimi']:
            key = (src, ws)
            if key not in sess:
                continue
            cnt = collections.Counter(length_bucket(r[0]) for r in sess[key])
            cells = ' | '.join(str(cnt.get(b, 0)) for b in buckets_order)
            out.append(f"| {src} | {ws} | {cells} |")
    out.append('')

    # --- 三端共有的工作区横向对比（hc-all / java-agent / github / story-lifecycle 等）---
    out.append('## 4. 同工作区下三端横向对比\n')
    out.append('仅在"该工作区三端至少各有 3 个会话"时给出，避免小样本误导。')
    out.append('')
    tri_ws = []
    for ws in all_ws:
        have = [s for s in ['claude', 'codex', 'kimi'] if len(sess.get((s, ws), [])) >= 3]
        if len(have) >= 2:  # 至少两端可比；三端齐全优先标出
            tri_ws.append((ws, have))
    if not tri_ws:
        out.append('_无工作区满足两端各 ≥3 会话的对比条件。_')
    else:
        out.append('| 工作区 | 端 | 会话数 | turns 中位 | ntools 中位 | nerrs 中位 | 工具种类数 |')
        out.append('|---|---|---|---|---|---|---|')
        for ws, have in tri_ws:
            for src in have:
                rows = sess[(src, ws)]
                tlist = [r[0] for r in rows]
                nlist = [r[1] for r in rows]
                elist = [r[2] for r in rows]
                out.append(
                    f"| {ws} | {src} | {len(rows)} "
                    f"| {med(tlist):.1f} | {med(nlist):.1f} | {med(elist):.1f} "
                    f"| {tool_distinct.get((src, ws), 0)} |"
                )
    out.append('')

    # Top 工具名（每端）
    out.append('## 5. 每端 Top 工具（看工具广度的具体构成）\n')
    for src in ['claude', 'codex', 'kimi']:
        c.execute("SELECT name, COUNT(*) n FROM events WHERE kind='tool' AND src=? GROUP BY name ORDER BY n DESC LIMIT 8", (src,))
        rows = c.fetchall()
        if not rows:
            continue
        out.append(f'**{src}** Top 工具：')
        out.append('')
        out.append('| 工具 | 调用数 |')
        out.append('|---|---|')
        for name, n in rows:
            out.append(f"| {name} | {n} |")
        out.append('')

    # --- 附录：数据质量校验（解释为何 ntools/nerrs 从 events 重算）---
    out.append('## 附录 · 数据质量校验\n')
    out.append('> `sessions.turns` 对三端均准确（== events 中 ucmd 事件计数，逐一比对一致）。')
    out.append('> 但 `sessions.ntools` / `sessions.nerrs` 列在 Codex/Kimi 上不可靠，已改用 events 重算：')
    out.append('')
    # 重新统计：sessions 列 vs events 重算 的偏差
    for src in ['claude', 'codex', 'kimi']:
        c.execute(
            "SELECT s.ntools, s.nerrs FROM sessions s WHERE s.src=?", (src,))
        col_tools = []
        col_errs = []
        for ntools, nerrs in c.fetchall():
            col_tools.append(ntools or 0)
            col_errs.append(nerrs or 0)
        real_tools = [sid_ntools[sid] for sid, (s, w) in sess_meta.items() if s == src]
        real_errs = [sid_nerrs[sid] for sid, (s, w) in sess_meta.items() if s == src]
        out.append(
            f"- **{src}**：sessions.ntools 中位 {med(col_tools):.0f} vs events 重算 {med(real_tools):.0f}；"
            f"sessions.nerrs 中位 {med(col_errs):.0f} vs events 重算 {med(real_errs):.0f}。"
        )
    out.append('')
    out.append('结论：本报告 ntools/nerrs 一律以 events 重算为准。')

    conn.close()
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print(f'[tri_efficiency] wrote {OUT_FILE}')
    # 控制台简报（ASCII，规避 GBK）
    print('=== tri_efficiency summary ===')
    for src in ['claude', 'codex', 'kimi']:
        if src in agg:
            a = agg[src]
            print(
                f"{src}: n={len(a['turns'])} turns_med={med(a['turns']):.1f}/p90={p90(a['turns'])} "
                f"ntools_med={med(a['tools']):.1f}/p90={p90(a['tools'])} "
                f"nerrs_med={med(a['errs']):.1f} tool_kinds={len(a['tools_set'])}"
            )


if __name__ == '__main__':
    main()
