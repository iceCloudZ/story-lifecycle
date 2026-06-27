import pickle, json, collections, os, sys
_PROJ=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,_PROJ)
from miner import config  # noqa: E402
from miner.common import ws_of  # noqa: E402
C=config.CACHE_DIR
E=pickle.load(open(os.path.join(C,'events.pkl'),'rb'))
S=json.load(open(os.path.join(C,'sessions.json'),encoding='utf-8'))
# 目标工作区短名（用于挑「成功轨迹」样本）；默认取 config.WORKSPACES[0] 的短名（hc-all），
# 可用 config.json 的 "primary_ws" 或环境变量 MINER_PRIMARY_WS 覆盖。
PRIMARY_WS = config._cfg.get('primary_ws') or os.environ.get('MINER_PRIMARY_WS') or ws_of(config.WORKSPACES[0])
meta={s['sid']:s for s in S}
by=collections.defaultdict(list)
for e in E: by[e['sid']].append(e)
# pick 3 successful claude sessions: turns 5-25, has tool+result+code, errs low, ws=PRIMARY_WS
cand=[]
for sid,evs in by.items():
    m=meta.get(sid,{})
    if not sid.startswith('claude'): continue
    if m.get('ws')!=PRIMARY_WS: continue
    turns=m.get('turns',0); errs=m.get('nerrs',0); ntools=m.get('ntools',0)
    has_code=any(e.get('kind')=='code' for e in evs)
    if 5<=turns<=25 and ntools>=10 and errs<=turns and has_code:
        cand.append((sid,turns,ntools,errs,len(evs)))
cand.sort(key=lambda x:(x[3],-x[2]))
def to_messages(sid):
    evs=by[sid]; msgs=[]; cur_role=None
    for e in evs:
        k=e.get('kind')
        if k=='ucmd':
            msgs.append({"role":"user","content":e.get('text','')})
        elif k=='atext':
            msgs.append({"role":"assistant","content":e.get('text','')})
        elif k=='tool':
            msgs.append({"role":"assistant","content":f"[tool_use:{e.get('name')}] "+(e.get('cmd') or '')})
        elif k=='result':
            msgs.append({"role":"user","content":f"[tool_result {'OK' if e.get('ok') else 'ERR'}] "+(e.get('text') or '')})
        elif k=='code':
            msgs.append({"role":"assistant","content":f"[write {e.get('name')}] "+(e.get('code') or '')[:300]})
    return msgs
out=["# 方向2 · 知识蒸馏 demo（脱敏 + SFT 格式）\n"]
out.append(f"> 候选「成功轨迹」(claude/{PRIMARY_WS}, 5-25轮, ≥10工具, 有代码, 低错误): **{len(cand)}** 个\n")
out.append("## 候选会话\n")
out.append("| sid | turns | tools | errs | events |")
out.append("|---|---|---|---|---|")
for sid,t,nt,ne,nv in cand[:8]:
    out.append(f"| {sid} | {t} | {nt} | {ne} | {nv} |")
if cand:
    sid=cand[0][0]
    msgs=to_messages(sid)
    out.append(f"\n## 样本：{sid} → SFT messages 格式（已脱敏，截前 12 条）\n")
    out.append("```json")
    out.append(json.dumps(msgs[:12],ensure_ascii=False,indent=2))
    out.append("```")
    out.append(f"\n（该会话共 {len(msgs)} 条 message，转 ShareGPT 后可直接喂 TRL SFTTrainer）")
out.append("\n## 价值判断\n")
out.append("- 管线已跑通：选轨迹→转 messages→脱敏→SFT 格式。")
out.append("- **硬门槛**：金融 PII/生产数据脱敏需人工复核（自动正则只能覆盖手机号/邮箱/长数字）。")
out.append(f"- 当前候选 {len(cand)} 条偏少，扩大到全工作区/放宽条件可获数百条。")
os.makedirs(C, exist_ok=True)
open(os.path.join(C,'d2_distill.md'),'w',encoding='utf-8').write('\n'.join(out))
print("d2 done; candidates:",len(cand))
