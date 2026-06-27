"""方向9 预测性分析（诚实版）：成败预测不可行，工作量预估可做粗估。

v2：新增按「工作区 × 任务类型 × story 复杂度」的 effort-estimate 表。
"""
import sqlite3, os, collections, statistics, sys

DB = 'D:/github/story-lifecycle/packages/story-miner/data/transcripts.db'
OUT = 'D:/github/story-lifecycle/packages/story-miner/scripts/out/predict.md'
OUT_EFFORT = 'D:/github/story-lifecycle/packages/story-miner/scripts/out/effort-estimate.md'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_playbooks import THEME  # noqa: E402


def med(x): return statistics.median(x) if x else 0


def p90(x):
    if not x:
        return 0
    s = sorted(x)
    return s[min(int(len(s) * 0.9), len(s) - 1)]


def infer_task_type(first_ucmd):
    fl = (first_ucmd or '').lower()
    for theme, (label, kws) in THEME.items():
        if any(k.lower() in fl for k in kws):
            return theme, label
    return 'other', '其他/未分类'


def build_effort_table(c):
    """按 (ws, task_type, complexity) 聚合 turns/tools 的中位/P90。

    口径：
    - 所有含 first_ucmd 的会话（不限 claude，覆盖主要任务类型）。
    - turns/tools 均从 events 重算，避免各端列值口径不一致。
    - 复杂度从 stories 表关联；无 story 或无数值时记为'未知'。
    """
    rows = c.execute(
        """SELECT s.sid, s.ws, s.first_ucmd, s.story_id, st.complexity
           FROM sessions s LEFT JOIN stories st ON s.story_id=st.story_id
           WHERE s.first_ucmd IS NOT NULL"""
    ).fetchall()

    groups_3d = collections.defaultdict(lambda: {'turns': [], 'tools': []})
    groups_2d = collections.defaultdict(lambda: {'turns': [], 'tools': []})

    for sid, ws, fu, story_id, complexity in rows:
        ntools = c.execute(
            "SELECT Count(*) FROM events WHERE sid=? AND kind='tool'", (sid,)
        ).fetchone()[0]
        if ntools <= 0:
            continue
        turns = c.execute(
            "SELECT Count(*) FROM events WHERE sid=? AND kind='ucmd'", (sid,)
        ).fetchone()[0]
        theme, label = infer_task_type(fu)
        cx = (complexity or '未知').strip()

        groups_3d[(ws, theme, label, cx)]['turns'].append(turns)
        groups_3d[(ws, theme, label, cx)]['tools'].append(ntools)
        groups_2d[(ws, theme, label)]['turns'].append(turns)
        groups_2d[(ws, theme, label)]['tools'].append(ntools)

    return groups_2d, groups_3d


def summarize(v):
    n = len(v['turns'])
    return n, med(v['turns']), p90(v['turns']), med(v['tools']), p90(v['tools'])


def table_row(ws, label, cx, n, mt, pt, mn, pn):
    return (f"| {ws} | {label} | {cx} | {n} | "
            f"{mt:.0f} | {pt:.0f} | {mn:.0f} | {pn:.0f} |")


def render_effort_table(groups_2d, groups_3d):
    out = ["# 工作量预估表（工作区 × 任务类型 × Story 复杂度）\n",
           "> 基于历史 transcript 的 turns/tools 中位/P90。"
           "turns/tools 均从 events 重算，避免各端列值口径差异。\n",
           "> ⚠️ **诚实声明**：方差普遍很大，P90 常数倍于中位；此表只能给量级区间，不能当精确排期。\n"]

    header = ["| 工作区 | 任务类型 | 复杂度 | 样本数 | turns 中位 | turns P90 | tools 中位 | tools P90 |",
              "|---|---|---|---|---|---|---|---|"]

    # 1) 主表：工作区 × 任务类型（不区分复杂度，覆盖无 story 关联的日常维护会话）
    out.append("## 主表：按工作区 × 任务类型（推荐优先使用）")
    out.extend(header)
    items_2d = []
    for (ws, theme, label), v in groups_2d.items():
        n, mt, pt, mn, pn = summarize(v)
        if n < 3:
            continue
        items_2d.append((n, ws, label, v))
    items_2d.sort(key=lambda x: (-x[0], x[1], x[2]))
    for n, ws, label, v in items_2d:
        mt, pt, mn, pn = med(v['turns']), p90(v['turns']), med(v['tools']), p90(v['tools'])
        out.append(table_row(ws, label, '—', n, mt, pt, mn, pn))

    # 2) 细化表：工作区 × 任务类型 × 复杂度（仅展示已知 S/M/L 的样本）
    known = [(k, v) for k, v in groups_3d.items() if k[3] in ('S', 'M', 'L')]
    if known:
        out.append("\n## 细化表：按工作区 × 任务类型 × Story 复杂度")
        out.extend(header)
        items_3d = []
        for (ws, theme, label, cx), v in known:
            n, mt, pt, mn, pn = summarize(v)
            if n < 2:
                continue
            items_3d.append((n, ws, label, cx, v))
        items_3d.sort(key=lambda x: (x[1], x[2], x[3]))
        for n, ws, label, cx, v in items_3d:
            mt, pt, mn, pn = med(v['turns']), p90(v['turns']), med(v['tools']), p90(v['tools'])
            out.append(table_row(ws, label, cx, n, mt, pt, mn, pn))

    # 3) 稀疏/不可估单元格说明
    sparse = [((ws, theme, label, cx), v)
              for (ws, theme, label, cx), v in groups_3d.items()
              if cx in ('S', 'M', 'L') and len(v['turns']) < 2]
    if sparse:
        out.append("\n## 数据稀疏说明")
        out.append("以下 `工作区 × 任务类型 × 复杂度` 组合样本<2，未列入细化表，仅供参考：")
        for (ws, theme, label, cx), v in sorted(sparse, key=lambda x: (x[0][0], x[0][1], x[0][3])):
            n, mt, pt, mn, pn = summarize(v)
            out.append(f"- {ws} / {label} / 复杂度 {cx}: n={n}, turns {mt:.0f}/{pt:.0f}, tools {mn:.0f}/{pn:.0f}")

    # 4) 用法建议
    out.append("\n## 用法建议")
    out.append("1. **首选主表**：按 `工作区 + 任务类型` 查基准区间；样本量最大，最稳定。")
    out.append("2. **复杂度校正**：若已知 story 复杂度（S/M/L），再用细化表校正；当前复杂度标注主要落在 `hc-all`，其他工作区样本不足。")
    out.append("3. **预留缓冲**：当 P90 远高于中位时，说明该类任务波动大，建议按 P90 给排期上界或拆分任务。")
    out.append("4. **数据局限**：当前 story 关联率约 18%，大量日常维护会话无 complexity；预估结果应随 story 关联率提升而迭代。")

    return out


def old_analysis(c):
    feat = []
    for sid, ws in c.execute("SELECT sid, ws FROM sessions WHERE src='claude'"):
        nt = c.execute("SELECT Count(*) FROM events WHERE sid=? AND kind='tool'", (sid,)).fetchone()[0]
        if nt <= 0:
            continue
        ne = c.execute("SELECT Count(*) FROM events WHERE sid=? AND kind='result' AND ok=0", (sid,)).fetchone()[0]
        uc = c.execute("SELECT Count(*) FROM events WHERE sid=? AND kind='ucmd'", (sid,)).fetchone()[0]
        feat.append((sid, ws, uc, nt, ne, ne / nt))
    turns = [f[2] for f in feat]
    nt_v = [f[3] for f in feat]
    ratio = [f[5] for f in feat]
    out = ["# 预测性分析（诚实结论）\n", f"样本：{len(feat)} 个 claude 会话（events 重算，列值不可靠）"]
    out.append(f"\n## 特征分布")
    out.append(f"- turns 中位/P90: {med(turns)}/{p90(turns)}")
    out.append(f"- ntools 中位/P90: {med(nt_v)}/{p90(nt_v)}")
    out.append(f"- 错误率(nerrs/ntools) 中位: {med(ratio):.2f}")
    with_err = sum(1 for f in feat if f[4] > 0)
    out.append(f"- 含错误的会话: {with_err}/{len(feat)} ({with_err*100//max(len(feat),1)}%)")
    out.append(f"\n## 分类预测（成败）— 不可行")
    hi = [f[2] for f in feat if f[5] > 0.3]
    lo = [f[2] for f in feat if f[5] < 0.1]
    out.append(f"- 错误率>0.3 的会话 {len(hi)} 个，turns 中位 {med(hi):.0f}")
    out.append(f"- 错误率<0.1 的会话 {len(lo)} 个，turns 中位 {med(lo):.0f}")
    out.append("- ❌ 无真实成败标签（agent 会话不记录成功/失败结局），错误率/turns 只能粗分'折腾程度'，**不能可靠预测成败**")
    out.append(f"\n## 回归预测（工作量）— 可做粗估")
    by_ws = collections.defaultdict(list)
    for f in feat:
        by_ws[f[1]].append(f[2])
    out.append("- 按 ws 的 turns 中位（粗估基线）:")
    for ws, v in sorted(by_ws.items()):
        if len(v) >= 3:
            out.append(f"  - {ws}: 中位 {med(v):.0f}，P90 {p90(v):.0f}（n={len(v)}）")
    out.append("- ⚠️ 方差大（P90 远高于中位），只能给量级，不能精确。要提升精度需更多特征（story 复杂度/服务数/共享状态）")
    out.append(f"\n## 结论")
    out.append("- 方向9「成败预测」：**当前数据不支持**，缺标签是硬伤（需手动标注一批会话结局才有监督信号）")
    out.append("- 方向9「工作量预估」：**可做粗估**（同 ws/同类任务给历史中位），已在 playbook/阶段成本画像里部分体现")
    return out


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    c = sqlite3.connect(DB)

    # 保留旧分析
    old_out = old_analysis(c)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(old_out))
    print(f"predict: old analysis -> {OUT}")

    # 新增 effort estimate
    groups_2d, groups_3d = build_effort_table(c)
    effort_out = render_effort_table(groups_2d, groups_3d)
    with open(OUT_EFFORT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(effort_out))
    print(f"predict: effort estimate -> {OUT_EFFORT}")


if __name__ == '__main__':
    main()
