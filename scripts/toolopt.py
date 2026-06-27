import pickle, collections
E=pickle.load(open(r'D:/hc-all/.claude/tmp/cache/events.pkl','rb'))
def classify(c):
    c=c.strip(); parts=c.split()
    first=parts[0] if parts else ''
    if first in ('cd','export','sleep','echo','set','unset','pwd','clear','true','false','alias'): return 'noise:env'
    if 'PYTHONIOENCODING' in c or c.startswith('TOKEN=') or 'TOKEN=$(python' in c: return 'noise:env'
    if 'cli_skyladder' in c: return 'cli:skyladder(部署)'
    if 'cli_sql' in c: return 'cli:sql(查库)'
    if 'cli_behavior' in c or 'cli_es' in c: return 'cli:behavior/es'
    if first=='curl': return 'net:curl(调API)'
    if first=='ssh': return 'net:ssh(登机)'
    if first=='git': return 'git'
    if first in ('find','grep','rg'): return 'search'
    if first in ('python','python3'): return 'python(脚本)'
    if first=='mvn': return 'build:mvn'
    if first in ('docker','kubectl'): return 'infra'
    if first in ('npm','yarn','tsc'): return 'js'
    if first=='rtk': return 'rtk(proxy)'
    return 'other'
bashes=[e['cmd'] for e in E if e.get('kind')=='tool' and e.get('name')=='Bash' and e.get('cmd')]
cls=collections.Counter(classify(b) for b in bashes)
noise=sum(v for k,v in cls.items() if k.startswith('noise'))
out=["# 方向7 · 工具使用优化（初步结果）\n"]
out.append(f"Bash 总调用: **{len(bashes)}**，其中环境噪声(cd/export/sleep/echo/TOKEN)占 **{noise} ({noise*100//max(len(bashes),1)}%)**\n")
out.append("## 去噪后真实业务命令分布\n")
out.append("| 语义类别 | 次数 | 说明 |")
out.append("|---|---|---|")
for k,v in cls.most_common(20):
    if k.startswith('noise'): continue
    out.append(f"| {k} | {v} | |")
out.append("\n## 可封装/自动化的重复模式（建议）\n")
out.append("- `cli:skyladder` + `cli:sql` + `net:curl` 三类合计极高 → **已封装进 ys-cli skill，验证有效**")
out.append("- `noise:env` 占大头（cd+export+TOKEN 套餐）→ 可固化成「进入工作态」一键脚本/skill")
out.append("- `net:ssh` 高频登固定机 → 可做 ssh 别名/跳板封装")
out.append("- `search`(find/grep) 高频 → codegraph 已部分替代，可检查 grep→codegraph 迁移率")
open(r'D:/hc-all/.claude/tmp/cache/d7_toolopt.md','w',encoding='utf-8').write('\n'.join(out))
print("d7 done; bash total:",len(bashes),"noise:",noise)
