#!/usr/bin/env python3
"""验证思路 B:compact 之后失败(is_error)是否聚类。

设计:对每个 compact 边界,取前后各 K 个 tool_result,配对比较 is_error 失败率。
配对(同一边界的前 vs 后)可控制"长会话本身更难"的会话级混淆。

只读 ~/.claude/projects 顶层 session jsonl(排除 subagents/ 与当前会话)。
不落盘任何内容,只输出聚合计数。
"""
import glob, json, os

ROOT = os.path.expanduser('~/.claude/projects')
CUR_SID = 'd9edd9a8-dfca-4ae2-9cb7-a049e0981cdb'  # 本会话,排除避免自污染
KS = [10, 20]


def tokens_of(path):
    """有序 token 列表: ('B',) compact 边界 / ('R', is_error) 工具结果。无边界返回 None。"""
    try:
        text = open(path, encoding='utf-8', errors='ignore').read()
    except Exception:
        return None
    if 'compact_boundary' not in text and 'compactMetadata' not in text:
        return None
    toks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        # compact 边界(system 事件 或 挂 compactMetadata 的续跑消息)
        if o.get('type') == 'system' and o.get('subtype') == 'compact_boundary':
            toks.append(('B',))
            continue
        if 'compactMetadata' in line and o.get('type') in ('system', 'user'):
            toks.append(('B',))
            continue
        msg = o.get('message')
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list):
            continue
        for p in content:
            if isinstance(p, dict) and p.get('type') == 'tool_result':
                toks.append(('R', bool(p.get('is_error'))))
    return toks


def windows(toks, K):
    """每个边界 -> (前失败数, 前样本数, 后失败数, 后样本数)。"""
    r_list = [(i, t[1]) for i, t in enumerate(toks) if t[0] == 'R']
    out = []
    for i, t in enumerate(toks):
        if t[0] != 'B':
            continue
        before = [v for (ii, v) in r_list if ii < i][-K:]
        after = [v for (ii, v) in r_list if ii > i][:K]
        out.append((sum(before), len(before), sum(after), len(after)))
    return out


def rate(f, n):
    return f / n if n else 0.0


def main():
    files = [f for f in glob.glob(os.path.join(ROOT, '*', '*.jsonl')) if CUR_SID not in f]
    per_K = {K: [] for K in KS}
    n_sess = n_boundary = 0
    for f in files:
        toks = tokens_of(f)
        if not toks or not any(t[0] == 'B' for t in toks):
            continue
        n_sess += 1
        n_boundary += sum(1 for t in toks if t[0] == 'B')
        for K in KS:
            per_K[K].extend(windows(toks, K))

    print(f"扫描 session 文件: {len(files)}")
    print(f"含 compact 边界的 session: {n_sess}")
    print(f"compact 边界总数(未去重相邻): {n_boundary}")
    print()
    for K in KS:
        ws = per_K[K]
        if not ws:
            print(f"K={K}: 无样本\n")
            continue
        bf, bn = sum(w[0] for w in ws), sum(w[1] for w in ws)
        af, an = sum(w[2] for w in ws), sum(w[3] for w in ws)
        br, ar = rate(bf, bn), rate(af, an)
        pos = sum(1 for w in ws if rate(*w[2:]) > rate(*w[:2]))
        neg = sum(1 for w in ws if rate(*w[2:]) < rate(*w[:2]))
        eq = len(ws) - pos - neg
        rel = f"{(ar - br) / br * 100:+.0f}%" if br else "n/a"
        print(f"K={K} (每边界前后各 {K} 个 tool_result)")
        print(f"  边界样本数: {len(ws)}")
        print(f"  前: {bf}/{bn} = {br:.1%}   后: {af}/{an} = {ar:.1%}   相对变化: {rel}")
        print(f"  配对方向: 后>前 {pos} | 后<前 {neg} | 相等 {eq}")
        print()


if __name__ == '__main__':
    main()
