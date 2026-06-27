"""方向3 智能推荐：给定任务关键词，生成任务上下文包。

用法:
  python recommend.py "免息 清分"            # 默认生成结构化推荐（兼容旧列表）
  python recommend.py "免息 清分" --package  # 生成 <500 字任务上下文包（story-lifecycle 注入）
"""
import sqlite3, sys, os, collections, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_playbooks import short, THEME, fail_class  # noqa: E402
from miner.common import mask  # noqa: E402

DB = 'D:/github/story-lifecycle/packages/story-miner/data/transcripts.db'
OUT = 'D:/github/story-lifecycle/packages/story-miner/scripts/out/recommend.md'


def classify_query(query):
    """返回所有匹配的主题（支持多主题查询如'免息 清分'）。"""
    ql = query.lower()
    matched = []
    for theme, (label, kws) in THEME.items():
        if any(k.lower() in ql for k in kws):
            matched.append((theme, label))
    return matched


def fetch_sessions(c, kws):
    match = []
    for sid, fu, turns, ntools, nerrs in c.execute(
        "SELECT sid, first_ucmd, turns, ntools, nerrs FROM sessions WHERE first_ucmd IS NOT NULL"
    ):
        fl = (fu or '').lower()
        if any(k.lower() in fl for k in kws):
            match.append((sid, mask(fu or ''), turns or 0, ntools or 0, nerrs or 0))
    return match


def fetch_files(c, sids):
    files = collections.Counter()
    if not sids:
        return files
    ph = ','.join('?' * len(sids))
    for (p,) in c.execute(
        f"SELECT path FROM events WHERE kind='tool' AND name IN ('Read','Grep','Glob') "
        f"AND COALESCE(path,'')<>'' AND sid IN ({ph})", sids
    ):
        pp = (p or '').replace('\\', '/')
        if pp.endswith(('.java', '.ts', '.tsx', '.sql')):
            files[pp] += 1
    return files


def fetch_fails(c, sids):
    fails = collections.Counter()
    if not sids:
        return fails
    ph = ','.join('?' * len(sids))
    for (t,) in c.execute(
        f"SELECT text FROM events WHERE kind='result' AND ok=0 AND COALESCE(text,'')<>'' AND sid IN ({ph})",
        sids,
    ):
        fc = fail_class(t)
        if fc:
            fails[fc] += 1
    return fails


def render_list(query, kws, match, files, rec_pb):
    out = [f"# 智能推荐 — 任务「{query}」\n", f"关键词: {kws}"]
    out.append(f"\n## 相关历史会话（first_ucmd 命中 {len(match)}）")
    for sid, fu, turns, ntools, nerrs in sorted(match, key=lambda x: -len(x[1] or ''))[:10]:
        out.append(f"- `{sid}` {(fu or '')[:55]} (turns={turns} tools={ntools} errs={nerrs})")
    out.append(f"\n## 必看文件（相关会话高频，{len(files)} 个）")
    for f, n in files.most_common(10):
        out.append(f"- `{short(f)}` ({n})")
    out.append("\n## 推荐 playbook")
    for pb in rec_pb:
        out.append(f"- `.story/knowledge/playbooks/{pb}.md`")
    return '\n'.join(out)


def render_context_package(query, kws, themes, match, files, fails):
    """生成 <500 字符的任务上下文包，格式对齐 story-lifecycle prompt 注入。

    以紧凑 bullet 形式输出：任务摘要 → 相关会话 → 必看文件 → 推荐 playbook
    → 常见踩坑。导出文本均经 mask() 脱敏；文件路径经 short() 规整。
    """
    labels = [label for _, label in themes]
    rec_pb = [theme for theme, _ in themes]

    # 任务摘要
    parts = [
        f"### 任务上下文包：{query}",
        f"- **任务类型：** {' / '.join(labels) if labels else '综合任务'}；"
        f"命中 {len(match)} 个历史 session。",
    ]

    # 相关会话：取工具数最高的前 2 个，sid 取第一段非 src 的 8 位
    top = sorted(match, key=lambda x: -(x[3] or 0))[:2]
    sess_items = []
    for sid, fu, turns, ntools, _nerrs in top:
        # sid 形如 claude:uuid 或 kimi:uuid:main，取第一段 uuid 前 8 位
        sid_parts = sid.split(':')
        short_sid = sid_parts[1][:8] if len(sid_parts) > 1 else sid[:8]
        snippet = (fu or '').strip().replace('\n', ' ')[:28]
        sess_items.append(f"`{short_sid}…` {snippet} ({turns}t/{ntools}tl)")
    if sess_items:
        parts.append("- **相关会话：** " + "；".join(sess_items) + "。")

    # 必看文件：取 top 2，用 short()
    file_items = []
    for f, n in files.most_common(2):
        file_items.append(f"`{short(f)}`×{n}")
    if file_items:
        parts.append("- **必看文件：** " + "、".join(file_items) + "。")

    # 推荐 playbook
    if rec_pb:
        pb_refs = [f"`{pb}.md`" for pb in rec_pb]
        parts.append("- **推荐 Playbook：** " + "、".join(pb_refs) + "。")

    # 常见踩坑：取 top 3
    pit_items = []
    for fc, n in fails.most_common(3):
        pit_items.append(f"{fc}×{n}")
    if pit_items:
        parts.append("- **常见踩坑：** " + "、".join(pit_items) + "。")

    # 起手动作（仅一条，避免超长）
    action = None
    if 'debug' in rec_pb:
        action = "先复现问题，再查日志/编译。"
    elif 'data-sql' in rec_pb:
        action = "先确认 SQL 影响范围与回滚方案。"
    elif 'credit-risk' in rec_pb or 'sms-marketing' in rec_pb:
        action = "重点检查对客文案、硬编码 ID、枚举一致性。"
    elif 'deploy' in rec_pb:
        action = "确认分支、依赖版本、配置差异。"
    elif 'frontend' in rec_pb:
        action = "确认组件库版本与接口契约。"
    if action:
        parts.append(f"- **起手动作：** {action}")

    parts.append("- 文件为历史高频访问，代码可能已变更，执行前用 codegraph 核验。")

    body = '\n'.join(parts)
    # 严格控制在 500 字符以内（story-lifecycle provider 合同）
    if len(body) > 490:
        # 截断到最近换行，保留头部
        cut = body[:490].rsplit('\n', 1)[0]
        body = cut + "\n- （上下文包已截断）"
    return body


def main():
    parser = argparse.ArgumentParser(description="智能推荐 / 任务上下文包")
    parser.add_argument("query", nargs="?", default="免息", help="任务关键词")
    parser.add_argument("--package", action="store_true", help="生成 concise 任务上下文包")
    args = parser.parse_args()

    query = args.query
    kws = [k.strip() for k in query.replace('+', ' ').split() if k.strip()]
    c = sqlite3.connect(DB)
    match = fetch_sessions(c, kws)
    msids = [s for s, _, _, _, _ in match]
    files = fetch_files(c, msids)
    fails = fetch_fails(c, msids)
    themes = classify_query(query)
    rec_pb = [theme for theme, _ in themes]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if args.package:
        out = render_context_package(query, kws, themes, match, files, fails)
        safe = query.replace(' ', '_').replace('/', '_')[:30]
        out_path = f'D:/github/story-lifecycle/packages/story-miner/scripts/out/context-package-{safe}.md'
    else:
        out = render_list(query, kws, match, files, rec_pb)
        out_path = OUT

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(out)
    word_count = len(out.split())
    print(f"recommend '{query}': package={args.package}, {len(match)} sessions, "
          f"{len(files)} files, pb={rec_pb}, words={word_count} -> {out_path}")


if __name__ == '__main__':
    main()
