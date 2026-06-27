"""方向1 自动复盘：给高活跃会话生成结构化复盘。

v2：支持 `python retrospect.py <sid>` 单会话模式；结构化输出（任务/做了什么/关键决策/踩坑/访问文件/结论）；
路径展示复用 generate_playbooks 的 short() 避免丢首字母。
"""
import sqlite3, os, collections, sys, re

DB = 'D:/github/story-lifecycle/packages/story-miner/data/transcripts.db'
OUT_DIR = 'D:/github/story-lifecycle/packages/story-miner/scripts/out'
OUT_BATCH = os.path.join(OUT_DIR, 'retrospect.md')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_playbooks import short, THEME  # noqa: E402


def infer_task_type(first_ucmd):
    fl = (first_ucmd or '').lower()
    for theme, (label, kws) in THEME.items():
        if any(k.lower() in fl for k in kws):
            return label
    return '综合任务'


def summarize_did(c, sid):
    """做了什么：按工具类型聚合，给出量化描述。"""
    tools = collections.Counter(
        r[0] for r in c.execute(
            "SELECT name FROM events WHERE sid=? AND kind='tool'", (sid,)
        )
    )
    tool_line = ', '.join(f"{n}×{t}" for t, n in tools.most_common(6))
    edits = c.execute(
        "SELECT Count(*) FROM events WHERE sid=? AND kind='code'", (sid,)
    ).fetchone()[0]
    bash_cmds = collections.Counter()
    for (cmd,) in c.execute(
        "SELECT cmd FROM events WHERE sid=? AND kind='tool' AND name='Bash'", (sid,)
    ):
        c_ = (cmd or '').strip()
        if c_.startswith('git'):
            bash_cmds['git'] += 1
        elif c_.startswith(('mvn', 'gradle', 'npm', 'yarn', 'tsc')):
            bash_cmds['build'] += 1
        elif 'test' in c_.lower():
            bash_cmds['test'] += 1
        else:
            bash_cmds['other'] += 1
    bash_line = ', '.join(f"{n}×{k}" for k, n in bash_cmds.most_common())
    summary = f"- 共调用 {sum(tools.values())} 次工具（{tool_line}）。"
    summary += f"\n- 写入 {edits} 处代码变更。"
    if bash_line:
        summary += f"\n- Bash 操作：{bash_line}。"
    return summary


def key_decisions(c, sid):
    """关键决策：从助手长文本里抽取含「决定/采用/选/方案」的句子。"""
    decs = []
    for (t,) in c.execute(
        "SELECT text FROM events WHERE sid=? AND kind='atext' AND length(text)>30 ORDER BY length(text) DESC LIMIT 20",
        (sid,),
    ):
        for sent in t.split('\n'):
            s = sent.strip()
            if any(k in s for k in ('决定', '采用', '选择', '方案', '结论', '建议', '最终')) and len(s) > 10:
                decs.append(s[:200])
    # 去重并截断
    seen = set()
    uniq = []
    for d in decs:
        key = d[:40]
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq[:5]


def visited_files(c, sid):
    """访问文件 Top，用 short() 展示。"""
    files = collections.Counter()
    disp = {}
    for (p,) in c.execute(
        "SELECT path FROM events WHERE sid=? AND kind='tool' AND name IN ('Read','Grep','Glob') AND COALESCE(path,'')<>''",
        (sid,),
    ):
        pnorm = p.replace('\\', '/')
        key = pnorm.lower()
        files[key] += 1
        if key not in disp:
            disp[key] = pnorm
    return [(disp[k], n) for k, n in files.most_common(10)]


def pitfalls(c, sid):
    """踩坑：失败 result 事件。"""
    errs = []
    for (name, text) in c.execute(
        "SELECT name, text FROM events WHERE sid=? AND kind='result' AND ok=0 AND COALESCE(text,'')<>'' ORDER BY id LIMIT 10",
        (sid,),
    ):
        t = (text or '').replace('\n', ' ')[:140]
        errs.append((name or 'tool', t))
    return errs


def conclusion(c, sid):
    """结论：从最后一段助手文本或标题推断。"""
    r = c.execute(
        "SELECT text FROM events WHERE sid=? AND kind='atext' ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if r and r[0]:
        return (r[0].replace('\n', ' ')[:200])
    return '（未提取到明确结论）'


def render_session(c, sid):
    r = c.execute(
        "SELECT first_ucmd, ws, ts, turns, ntools, nerrs, title, story_id FROM sessions WHERE sid=?",
        (sid,),
    ).fetchone()
    if not r:
        return None, f"未找到 session: {sid}"
    fu, ws, ts, turns, ntools, nerrs, title, story_id = r
    task_type = infer_task_type(fu)
    lines = [f"# Session 复盘 — `{sid}`", ""]
    lines.append(f"**任务：** {(fu or title or '无标题')[:80]}")
    lines.append(f"**类型：** {task_type} | **工作区：** {ws} | **时间：** {ts}")
    lines.append(f"**指标：** turns={turns} tools={ntools} errs={nerrs} story_id={story_id or '—'}")
    lines.append("")

    lines.append("## 任务\n")
    lines.append((fu or '（first_ucmd 为空）'))
    lines.append("")

    lines.append("## 做了什么\n")
    lines.append(summarize_did(c, sid))
    lines.append("")

    lines.append("## 关键决策\n")
    decs = key_decisions(c, sid)
    if decs:
        for d in decs:
            lines.append(f"- {d}")
    else:
        lines.append("- （未识别到明确决策句）")
    lines.append("")

    lines.append("## 踩坑\n")
    errs = pitfalls(c, sid)
    if errs:
        for name, e in errs[:6]:
            lines.append(f"- `{name}`: {e}")
    else:
        lines.append("- （无明显错误 result）")
    lines.append("")

    lines.append("## 访问文件\n")
    files = visited_files(c, sid)
    if files:
        lines.append("| 文件 | 次数 |")
        lines.append("|---|---|")
        for f, n in files:
            lines.append(f"| `{short(f)}` | {n} |")
    else:
        lines.append("- （无访问文件记录）")
    lines.append("")

    lines.append("## 结论\n")
    lines.append(f"- {conclusion(c, sid)}")
    return sid, '\n'.join(lines)


def batch_top5():
    os.makedirs(OUT_DIR, exist_ok=True)
    c = sqlite3.connect(DB)
    sids = [r[0] for r in c.execute(
        "SELECT sid FROM sessions WHERE src='claude' AND ws='hc-all' ORDER BY ntools DESC LIMIT 5"
    )]
    out = ["# 自动复盘（高活跃会话 Top5）\n",
           "> 数据驱动复盘：从 transcript 自动提取任务/工具/访问文件/踩坑/关键输出。"]
    for sid in sids:
        _, body = render_session(c, sid)
        out.append(body)
        out.append("\n---\n")
    with open(OUT_BATCH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print(f"retrospect batch: {len(sids)} sessions -> {OUT_BATCH}")


def safe_filename(sid):
    """Windows 文件名不能含 :/\\ 等字符；替换成下划线。"""
    return re.sub(r'[:/\\<>"|?*]', '_', sid)


def single(sid):
    c = sqlite3.connect(DB)
    _, body = render_session(c, sid)
    out_path = os.path.join(OUT_DIR, f'retrospect_{safe_filename(sid)}.md')
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(body)
    print(f"retrospect single: {sid} -> {out_path}")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        single(sys.argv[1])
    else:
        batch_top5()
