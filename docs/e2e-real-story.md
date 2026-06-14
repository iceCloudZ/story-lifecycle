# 用 Playwright 真实跑通「开始开发」端到端

一份自包含指南(可直接交给另一个 AI 执行)。用 Playwright 驱动一个真实 TAPD story 走完:开始开发 → 关联项目 + 填 PRD → LLM 规划 → 确认计划 → **claude CLI 在项目仓库里执行**。

---

## 项目背景
- 路径 `D:\story-lifecycle`,AI 编排器(后端 Python/FastAPI,前端 React 已构建)。
- 后端 `story serve --port 8180` **同时**服务 API 和前端(打开 http://127.0.0.1:8180 就是应用)。
- 流程:选 TAPD 需求 story →「开始开发」→ 关联项目 + **填 PRD(必填)** → 确认 → LLM(DeepSeek)规划 → 确认计划 → claude CLI 在项目仓库执行。
- 当前配置:minimal profile 的 design/implement 都指定 `cli: claude`(claude/codex 都可选,但 profile 指定 claude)。执行阶段**应该起 claude,不是 codex**。

## ⚠️ 必须先知道的坑(否则绕大弯)
1. **RTK 会过滤 shell 输出!** `git status`/`find`/`pytest`/`tasklist | grep` 的输出会被 RTK 代理吞掉或改写(`git status` 变 `ok`、`pytest` 变 "No tests collected")。**看真实输出用 `rtk proxy <cmd>`**,或用 **Glob/Grep/Read 专用工具**,或用 **powershell** 数进程。别信"太干净"的输出。
2. **规划是前端详情页开 SSE 触发的**——光 POST /start 不会规划。必须**用 Playwright 打开 `/story/{key}` 详情页**,SSE 才生成 plan。
3. **确认计划(plan/confirm)别太早**——先轮询 `GET /plan` 直到 `actions.length > 0`(规划完成),再 confirm。否则 0 action、不执行。
4. **claude 要已登录**(Anthropic 认证)。没登录执行直接报错。先确认。
5. **DeepSeek key 要有效**(规划用,config 里有)。
6. CLI 会在**绑定的项目仓库里改文件**——绑 happy-cash(`D:\hc-all`)就改 hc-all。挑个安全的 repo。

## 步骤

### 0. 准备 Playwright(chromium 已装,不用下载)
```bash
mkdir -p /c/Users/zzh58/AppData/Local/Temp/sl-e2e && cd $_
npm init -y >/dev/null && npm install playwright   # 复用已缓存浏览器
```

### 1. 起后端(后台)
```bash
rtk proxy story serve --port 8180   # 等 6s 起
```

### 2. 写并运行 E2E 脚本
挑 candidate story → 浏览器 fetch `/start`(project_ids=[2]=happy-cash + PRD)→ 打开详情页触发 SSE 规划 → 轮询 `/plan` 等 actions → confirm → 观察 PTY + prompt 文件 → abort。

```js
// e2e.mjs —— 保存到上面那个 temp 目录
import { chromium } from 'playwright'
import { readFileSync, existsSync } from 'fs'
const BASE='http://127.0.0.1:8180'
const MARKER='PRD_MARKER_7741'
const PRD=`# 需求\n${MARKER} 登录记录查询：字段含用户/IP/时间/状态，支持筛选分页。`
const note=m=>console.log(`[${new Date().toISOString().slice(11,19)}] ${m}`)
const api=p=>fetch(`${BASE}${p}`).then(r=>r.json()).catch(()=>null)
const browser=await chromium.launch()
const page=await(await browser.newContext({viewport:{width:1440,height:900}})).newPage()
await page.goto(BASE,{waitUntil:'domcontentloaded'}); await page.waitForTimeout(1200)
// 挑一个 candidate story
const all=await api('/api/story?show_all=true')
const cand=(all||[]).find(s=>s.intakeState==='candidate'&&s.status==='idle'&&s.tapdType==='story')
note(`candidate: ${cand?.storyKey}`); if(!cand){console.log('NO CANDIDATE');process.exit(1)}
const KEY=cand.storyKey
// /start(带 PRD,project=2=happy-cash)。浏览器内 fetch,等同 UI 点确认
const r=await page.evaluate(async a=>{const x=await fetch(`/api/story/${a.k}/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_ids:[2],content:a.prd})});return x.status},{k:KEY,prd:PRD})
note(`/start -> ${r}`)
await page.goto(`${BASE}/story/${KEY}`,{waitUntil:'domcontentloaded'})  // 关键:开详情页触发 SSE 规划
// 等规划出 actions(最多 75s)
let acts=0; for(let i=0;i<25;i++){await page.waitForTimeout(3000);const pl=await api(`/api/story/${KEY}/plan`);acts=pl?.actions?.length||0;if(acts>0){note(`plan ready: ${acts} actions, adapters=${pl.actions.map(a=>a.adapter).join(',')}`);break}}
if(!acts){note('planning 0 actions(DeepSeek 可能失败)');await fetch(`${BASE}/api/story/${KEY}/abort`,{method:'POST'});process.exit(2)}
const s0=await api(`/api/story/${KEY}`)
const prdPath=(JSON.parse(s0.contextJson||'{}').prd_path||'').replace(/\\/g,'/')
const ws=s0.workspace
note(`workspace=${ws} prd_path=${prdPath}`)
note(`confirm -> ${await fetch(`${BASE}/api/story/${KEY}/plan/confirm`,{method:'POST'}).then(x=>x.status)}`)
// 观察 PTY + prompt 文件
let ptyUp=false,promptHas=null
for(let i=0;i<12;i++){await page.waitForTimeout(2500)
  const sess=await api(`/api/story/${KEY}/sessions`)
  if((sess?.sessions?.length||0)>0&&!ptyUp){ptyUp=true;note(`PTY up: ${sess.sessions[0].adapter}`)}
  const pp=`${ws}/.story/context/${KEY}/prompt_design.md`.replace(/\\/g,'/')
  if(existsSync(pp)&&!promptHas){const t=readFileSync(pp,'utf-8');promptHas={path_in:t.includes(prdPath),marker_in:t.includes(MARKER),len:t.length};note(`prompt: len=${promptHas.len} path_in=${promptHas.path_in} marker_in=${promptHas.marker_in}`)}
  if(ptyUp&&promptHas)break}
await fetch(`${BASE}/api/story/${KEY}/abort`,{method:'POST'}); note('aborted')
await browser.close()
console.log('\nRESULT',JSON.stringify({KEY,prdPath,ptyUp,promptHas},null,2))
```
运行:`node e2e.mjs`

### 3. 观察 + 报告这些
- **PTY up 的 adapter 是不是 `claude`**(不是 codex)。
- `prompt_design.md`:`path_in=true`(PRD 路径注入)、`marker_in=false`(内容不内联)。
- 看**后端日志**(background task 输出文件)有没有 `EXECUTE stage=design adapter=claude` + `PTY session started` + `injecting prompt ... contains-context=...`。
- 故事终态:`GET /api/story/{KEY}` 的 status。

### 4. 收尾(重要)
- abort 后**清残留 CLI 进程**(claude 可能漏 helper):`taskkill /F /IM claude.exe`(或 codex.exe);用 powershell 数:`(Get-Process claude,Codex -ErrorAction SilentlyContinue).Count`。
- 停后端。

## 如果 candidate 用完了(都被 start 掉)
reset 一个回 candidate:
```bash
python -c "from story_lifecycle.db import models as db; db.update_story('<key>', status='idle', intake_state='candidate', context_json='{}')"
```

## 如果要提交/推送而 github.com 连不上
用 43 服务器开 SOCKS 隧道:
```bash
ssh -f -N -D 1080 43                                       # 开隧道
git -c http.proxy=socks5h://127.0.0.1:1080 push origin main # 推 main
git -c http.proxy=socks5h://127.0.0.1:1080 push origin <tag># 推 tag
HTTPS_PROXY=socks5h://127.0.0.1:1080 gh run watch <id>      # gh 也走代理
taskkill /F /PID <ssh-pid>                                  # 用完关隧道
```
先测隧道:`curl --proxy socks5h://127.0.0.1:1080 -o /dev/null -w '%{http_code}' https://github.com` → 200。PyPI 没被墙,pip 直连。

## 期望结果(PASS 判据)
- `ptyUp=true` 且 adapter=`claude`。
- `promptHas.path_in=true`、`promptHas.marker_in=false`。
- 后端日志有 `adapter=claude` 的 EXECUTE + PTY started。
- 进程清干净(claude.exe 计数归 0 或可接受)。
