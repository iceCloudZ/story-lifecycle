import json, statistics, collections
S=json.load(open(r'D:/hc-all/.claude/tmp/cache/sessions.json',encoding='utf-8'))
def med(x): return statistics.median(x) if x else 0
def p90(x):
    if not x: return 0
    x=sorted(x); return x[min(int(len(x)*0.9),len(x)-1)]
out=["# 方向4 · 工作量模式（初步结果）\n"]
out.append("## 各端 × 各工作区 对话成本\n")
out.append("（turns=用户轮数，tools=工具调用数，errs=错误事件数）\n")
out.append("| 端 | 工作区 | 会话数 | turns中位 | turns P90 | tools中位 | errs中位 |")
out.append("|---|---|---|---|---|---|---|")
grp=collections.defaultdict(list)
for s in S: grp[(s['src'],s['ws'])].append(s)
for (src,ws),ss in sorted(grp.items()):
    if len(ss)<3: continue
    out.append(f"| {src} | {ws} | {len(ss)} | {med([x['turns'] for x in ss]):.0f} | {p90([x['turns'] for x in ss]):.0f} | {med([x['ntools'] for x in ss]):.0f} | {med([x['nerrs'] for x in ss]):.0f} |")
TYPE=[('部署/上线',['deploy','部署','上线','skyladder','nexus','发版']),
      ('排查/Debug',['debug','排查','为什么','报错','bug','日志','失败']),
      ('需求开发',['实现','编码','feature','spec','story','tapd','设计文档']),
      ('SQL/数据',['sql','查询','数据','db','schema']),
      ('Skill/工具',['skill','mcp','blueprint','odps']),
      ('前端',['前端','admin','页面','protable','proform']),
      ('API/接口',['接口','api','feign','dto'])]
bucket=collections.defaultdict(lambda:[0,0,0])
for s in S:
    f=(s.get('first_ucmd') or '').lower()
    for name,kws in TYPE:
        if any(k.lower() in f for k in kws):
            b=bucket[name]; b[0]+=1; b[1]+=s['turns']; b[2]+=s['ntools']; break
out.append("\n## 任务类型 × 对话成本（哪类任务最耗对话/工具）\n")
out.append("| 任务类型 | 会话数 | 平均turns | 平均tools |")
out.append("|---|---|---|---|")
rows=[(n,c,round(t/c,1) if c else 0,round(to/c,1) if c else 0) for n,(c,t,to) in bucket.items() if c>0]
for n,c,avg,avgt in sorted(rows,key=lambda x:-x[2]):
    out.append(f"| {n} | {c} | {avg} | {avgt} |")
open(r'D:/hc-all/.claude/tmp/cache/d4_workload.md','w',encoding='utf-8').write('\n'.join(out))
print("d4 done; groups:",len(grp),"task types:",len([r for r in rows]))
