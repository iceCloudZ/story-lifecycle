"""持久化后的探索性挖掘：找 9 方向之外的新方向。纯 SQL/聚合，不展开实现。"""
import sqlite3, re, collections, statistics
DB=r'D:/hc-all/.claude/tmp/transcripts.db'
conn=sqlite3.connect(DB); conn.row_factory=sqlite3.Row
out=["# 持久化后探索：新方向信号探测\n"]

# 1) 任务分片/接力：同一 story 跨多个会话
S=[dict(r) for r in conn.execute('SELECT * FROM sessions')]
story=collections.defaultdict(list)
for s in S:
    m=re.search(r'((?:tapd-|STORY-)\d{5,})', (s['first_ucmd'] or '')+(s['title'] or ''), re.I)
    if m: story[m.group(1)].append(s)
multi={k:v for k,v in story.items() if len(v)>=2}
out.append(f"## 候选新方向①：任务分片/接力\n")
out.append(f"识别到带 story-id 的会话: {sum(len(v) for v in story.values())}；跨 ≥2 会话的 story: **{len(multi)}**\n")
out.append("| story-id | 会话数 | 涉及端 | 总turns |")
out.append("|---|---|---|---|")
for k,v in sorted(multi.items(),key=lambda x:-len(x[1]))[:12]:
    srcs=','.join(sorted(set(x['src'] for x in v)))
    out.append(f"| {k} | {len(v)} | {srcs} | {sum(x['turns'] for x in v)} |")

# 2) 卡住/重试循环：单会话单工具连续调用
out.append(f"\n## 候选新方向②：卡住/重试检测\n")
retry=conn.execute('''SELECT sid, name, Count(*) c FROM events WHERE kind='tool'
  GROUP BY sid,name HAVING c>=6 ORDER BY c DESC LIMIT 12''').fetchall()
out.append(f"单会话单工具调用 ≥6 次（疑似重试/循环）: **{len(retry)}** 组\n")
out.append("| sid | 工具 | 次数 |")
out.append("|---|---|---|")
for r in retry: out.append(f"| {r['sid']} | `{r['name']}` | {r['c']} |")

# 3) 三端同工作区效率对比
out.append(f"\n## 候选新方向③：三端效率对比（同工作区）\n")
out.append("| 工作区 | 端 | 会话 | avg turns | avg tools | avg errs |")
out.append("|---|---|---|---|---|---|")
for ws in ['hc-all','java-agent','story-lifecycle']:
    for r in conn.execute("SELECT src,Count(*),ROUND(Avg(turns),1),ROUND(Avg(ntools),1),ROUND(Avg(nerrs),1) FROM sessions WHERE ws=? AND turns>0 GROUP BY src",(ws,)).fetchall():
        out.append(f"| {ws} | {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")

# 4) 高错误率会话（疑似反复踩坑）
out.append(f"\n## 候选新方向④：反复踩坑会话\n")
hr=conn.execute('''SELECT ws,first_ucmd,turns,ntools,nerrs,ROUND(nerrs*1.0/MAX(ntools,1),2) r
  FROM sessions WHERE ntools>=15 ORDER BY r DESC LIMIT 10''').fetchall()
out.append("| 工作区 | 首条指令 | turns | tools | errs | 错误率 |")
out.append("|---|---|---|---|---|---|")
for r in hr: out.append(f"| {r['ws']} | {(r['first_ucmd'] or '')[:40]} | {r['turns']} | {r['ntools']} | {r['nerrs']} | {r['r']} |")

# 5) 工具失败文本聚类
out.append(f"\n## 候选新方向⑤：高频失败点（result ERR 文本聚类）\n")
fails=[r['text'] for r in conn.execute("SELECT text FROM events WHERE kind='result' AND ok=0 AND length(text)>5").fetchall()]
fc=collections.Counter()
KW={'权限/permission':['permission','denied','forbidden','权限','无权'],
    '不存在/notfound':['not found','不存在','no such','404'],
    '超时/timeout':['timeout','timed out','超时'],
    '语法/syntax':['syntax','unexpected','parse','语法'],
    '网络/network':['connection','refused','econn','network','网关','502','503'],
    '类型/type':['typeerror','nullpointer','classcast','illegalarg']}
for t in fails:
    tl=t.lower()
    for k,kws in KW.items():
        if any(kw in tl for kw in kws): fc[k]+=1; break
out.append(f"失败结果事件总数: **{len(fails)}**\n")
out.append("| 失败类别 | 次数 |")
out.append("|---|---|")
for k,c in fc.most_common(): out.append(f"| {k} | {c} |")

# 6) 工作日历：高强度日期
out.append(f"\n## 候选新方向⑥：工作强度日历\n")
bd=conn.execute("SELECT substr(ts,1,7) m,Count(*) FROM sessions WHERE ts LIKE '2026%' GROUP BY m ORDER BY m").fetchall()
out.append("| 月份 | 会话数 |")
out.append("|---|---|")
for r in bd: out.append(f"| {r[0]} | {r[1]} |")
topdays=conn.execute("SELECT ts,Count(*) c FROM sessions WHERE ts LIKE '2026%' GROUP BY ts ORDER BY c DESC LIMIT 5").fetchall()
out.append("最忙日期 Top5: " + ", ".join(f"{r['ts']}({r['c']})" for r in topdays))

open(r'D:/hc-all/.claude/tmp/cache/explore.md','w',encoding='utf-8').write('\n'.join(out))
print("explore done; multi-story:",len(multi),"retry-groups:",len(retry),"fails:",len(fails))
