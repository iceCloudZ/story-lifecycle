import pickle, collections, os, sys
_PROJ=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,_PROJ)
from miner import config  # noqa: E402
C=config.CACHE_DIR
E=pickle.load(open(os.path.join(C,'events.pkl'),'rb'))
tools=[(e['ts'],e['name']) for e in E if e.get('kind')=='tool' and e.get('ts') and e['ts'][0].isdigit()]
first=collections.defaultdict(lambda:'9999-99-99')
for ts,nm in tools:
    if ts<first[nm]: first[nm]=ts
out=["# 方向8 · 学习曲线（初步结果）\n"]
out.append("## 工具/MCP 首次采纳时间线（按首次出现排序）\n")
out.append("| 首次日期 | 工具 |")
out.append("|---|---|")
for nm,ts in sorted(first.items(),key=lambda x:x[1]):
    if nm.startswith('mcp__'):
        out.append(f"| {ts} | `{nm}` |")
out.append("\n## 月度新工具采纳数（学习速度）\n")
monthly=collections.Counter()
seen=set()
for ts,nm in sorted(tools):
    if nm not in seen:
        seen.add(nm); monthly[ts[:7]]+=1
out.append("| 月份 | 当月首次采纳的新工具数 |")
out.append("|---|---|")
for m in sorted(monthly): out.append(f"| {m} | {monthly[m]} |")
out.append("\n## 工具使用成熟度（首次试用 vs 近期频次）\n")
out.append("（近期7天日均调用 ≥10 视为「已熟练」）\n")
recent=collections.Counter(nm for ts,nm in tools if ts>='2026-06-19')
out.append("| 工具 | 首次采纳 | 近7天调用 | 状态 |")
out.append("|---|---|---|---|")
rows=[]
for nm in sorted(first,key=lambda x:first[x]):
    rc=recent.get(nm,0)
    status='🟢熟练' if rc>=70 else ('🟡在用' if rc>=10 else ('🔵试用' if rc>0 else '⚪搁置'))
    rows.append((first[nm],nm,rc,status))
for ts,nm,rc,st in sorted(rows,key=lambda x:-x[2])[:20]:
    out.append(f"| {ts} | `{nm}` | {rc} | {st} |")
os.makedirs(C, exist_ok=True)
open(os.path.join(C,'d8_learn.md'),'w',encoding='utf-8').write('\n'.join(out))
print("d8 done; tools tracked:",len(first))
