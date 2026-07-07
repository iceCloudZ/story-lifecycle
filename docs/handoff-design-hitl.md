# Handoff — design 阶段 HITL(交互式终端方向)| 续作文档

> 上一窗口上下文已满,本文档供新窗口无缝接续。读完即可继续。
> 起点日期 2026-07-07。仓库 `D:\github\story-lifecycle`(main,已 push)。

## TL;DR(现在在哪)

给 story-lifecycle 的 **design 阶段**做「claude 逐问 + 人答」HITL。绕了三圈,最终方向 =
**交互式终端**:design 跑交互式 claude(`["claude"]`,非 `-p`),前端 xterm 终端已双向接好,
**人实时 watch + Esc 打断 + 打字纠偏**(省 token 的真 HITL)。卡点 = spawn 后**自动注入
design 提示词**到 claude 的 TUI——Ink 把裸 PTY 写入当 paste 不 submit(claude-code#15553)。
最新尝试 = **bracketed paste**(`\x1b[200~ 文本 \x1b[201~` + `\r`),已 commit **但未 live 验证**。

**下个窗口第一件事** = live 验证 bracketed-paste 注入(见 §6)。

## 1. 目标 & 方向演进(三圈)

1. **侧文件协议**(`-p` headless):claude 写 `clarify_request.json` 后停。**已推翻**——`-p` 无
   AskUserQuestion(实测)、自主跑浪费 token(人不能 watch/interrupt/steer)。
2. **外接 MCP clarify**(headless `-p` + in-process/外接 MCP 工具):claude 调
   `mcp__lifecycle__clarify` 阻塞等人答。**已实现+验证(live PASS)**,但仍是自主 `-p`,不留作
   design 主路径(留给「人不在」的自主场景 / build·verify)。
3. **交互式终端**(当前方向):claude 交互式跑,人 steer。**前端终端已双向(xterm onData→ws→
   pty.stdin)**,只差 spawn 时自动注入提示词。

## 2. 仓库 & 关键文件

- 仓库:`D:\github\story-lifecycle`,main 分支,venv `.venv-monorepo-test`(用它的 python)。
- serve:`python -m story_lifecycle serve`(8180,**自带前端**(entry/web 构建产物),不需 vite;
  `http://127.0.0.1:8180/` 直接访问 UI)。用户的 serve 启动脚本:`C:\Users\zzh58\Desktop\启动story-serve.bat`。
- pytest:`./.venv-monorepo-test/Scripts/python.exe -m pytest packages/story-lifecycle/tests/`(别用项目 .venv)。
- 关键文件:
  - `orchestrator/service/api.py` — `_ensure_story_agent_pty`(交互终端 spawn + 注入,**当前卡点在这**)、`_build_interactive_stage_prompt`(构建设计 prompt)、`/clarify` 端点。
  - `orchestrator/engine/planner.py::_build_cli_prompt` + `prompt_sections.py::build_design_dimensions_section`(design prompt 文案,含「调 mcp__lifecycle__clarify」协议)。
  - `orchestrator/mcp/clarify_server.py` — 外接 MCP clarify 工具(MCP 方案,已验证)。
  - `infra/terminal/pty.py` — `ensure_agent_pty`(spawn + readiness + 注入)、`_wait_ready`。**铁律:不动 pty.py**。
  - 前端:`frontend/src/components/TerminalPanel.tsx`(xterm 双向:`onData→ws.send`、`ws.onmessage→term.write`)。前端是构建产物(entry/web),改前端要 `npm run build` 重新构建才在 8180 生效。
- webbridge(kimi-webbridge):本地 daemon `http://127.0.0.1:10086`,驱动用户真实浏览器。Windows 必须 **file-body** 传 JSON(curl.exe --data-binary @file),别 inline(中文变 `?`)。

## 3. 已 commit(都在 main,已 push)

```
5e65535c fix(pty): 注入用 bracketed paste + \r 提交          ← 未 live 验证(下一步)
c6dda715 fix(pty): 注入分两次写(text→延迟→\r)+ readiness_timeout=180  ← 上一版(split-write,没成)
a3924b72 fix(pty): 注入改「写文件+单行读文件指令」           ← prompt 写 .story/context/<key>/prompt_<stage>.md
1e295d0c fix(pty): spawn 传 readiness_marker(否则 2s sleep 写早被丢)
522d26a8 fix(pty): spawn 自动注入 stage prompt(_build_interactive_stage_prompt)
8450700a test(mcp): clarify_server stdio 全链路集成测
338d4ae7 refactor(clarify): 改外接 MCP clarify(事件驱动)     ← MCP 方案主提交
71dfc26f feat(mcp): clarify_server 核心
e3e99223 feat(frontend): ClarifyDialog(MCP 方案用)
```
(更早的侧文件 5 commit 已被 338d4ae7 替换/删除。)

## 4. 哪些验证过 / 哪些没成(诚实)

✅ **MCP clarify 全链路**(headless):claude 真调 `mcp__lifecycle__clarify` + 用返回值继续(live PASS,
cron 自动重跑命中网关恢复时过的)。回归 816 passed。
✅ **前端终端双向**:xterm `onData→ws.send` + 后端 `recv_and_write→pty.write` + `read_and_send`。已核。
✅ **spawn 跑的是交互式 claude**:`_ensure_story_agent_pty` → `adapter.interactive_launch_cmd` → `["claude"]`。
✅ **prompt 构建**:`_build_interactive_stage_prompt` 为 manual-emergency2 生成 7099 字符 prompt(含需求/PRD/设计协议/done 握手),写进 `.story/context/manual-emergency2/prompt_design.md`。
✅ **readiness fix**:spawn 传 readiness_marker=`❯`(claude adapter),readiness_timeout=180。
❌ **自动注入提交**(当前卡点):claude boot 到 `❯` 后,prompt 没被 submit(claude idle)。
   - 4 次尝试都失败 live:多行 `text+\r` 一次写 / split-write / 单行读文件指令 / readiness。
   - 根因(查证):claude Code 用 Ink,`ink-text-input` 把**裸 PTY 写入当 paste,不触发 submit**(claude-code#15553)。
   - 最新 fix(bracketed paste,5e65535c)**未 live 验证**。

## 5. 立即下一步:live 验证 bracketed-paste 注入

`5e65535c` 的注入逻辑(`_ensure_story_agent_pty` 末尾):
```python
pty.write(b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~")  # bracketed paste 填输入框
time.sleep(0.4)
pty.write(b"\r")                                                  # keystroke 提交
```
(prompt = 单行「读取 `.story/context/<key>/prompt_<stage>.md` 并执行」)

**验证步骤**(用户开 serve,我用 webbridge):
1. 确认 serve 跑最新代码(用户跑 `启动story-serve.bat`,它 kill 旧 8180 + 起 venv python serve)。
2. webbridge kill 旧 PTY:`curl ... /api/pty/manual-emergency2` DELETE。
3. webbridge `evaluate location.reload()` 重载 `http://127.0.0.1:8180/story/manual-emergency2?tab=terminal`(注意是 **8180 不是 5174**;同 URL 不 remount,要 reload)。
4. 页面出「暂无 CLI 会话 + 启动终端」→ `evaluate` 点启动终端(`[...buttons].find(b=>b.textContent.includes('启动终端')).click()`)。
5. **等 ~200s**(hc-all boot ~100s + readiness + 注入 + claude 读文件开干)。
6. 读终端:`evaluate document.querySelector('.xterm-rows').innerText`。
   - **成功**:看到 claude 读 PRD / Read 工具 / 设计内容(不再是空白 `❯`)。
   - **失败**:仍空白 `❯` → bracketed paste 也没成 → 见 §7 退路。

**注意 webbridge 坑**:Windows file-body 传 JSON;session 名固定 `sl-ui-test`;ref 会 stale(用 `evaluate` 按 text 找元素更稳);xterm 可能用 canvas 渲染(读 `.xterm-rows` 可能空,截图更可靠)。

## 6. 如果 bracketed paste 也不成(退路)

研究指向的其它可靠法(优先级):
1. **逐字符写入**(模拟真 keystroke,避开 paste 检测):`for ch in prompt: pty.write(ch); sleep(0.02)` 末尾 `\r`。慢但稳。最可能在 `_ensure_story_agent_pty` 里实现(pty.write 逐字符)。
2. **前端驱动注入**(最稳,但要改前端 + 重新 build):TerminalPanel 连上 + 检测到 `❯` 渲染后,前端 `ws.send(line + '\r')`——走用户打字的同一路径(proven 可 submit)。改 `TerminalPanel.tsx` + `npm run build` 更新 entry/web。
3. **AgentPTY 方案**(github.com/quietforgelabs/AgentPTY):「sends prompt to interactive claude + returns response」——参考它的注入实现(可能就是 bracketed paste 或逐字符)。
4. 兜底:claude Code 官方推荐程序化用 **headless/Agent SDK**(不走 TUI)——但那就回到自主模式,失掉交互 steer。

## 7. 其它已知坑 / 上下文

- **claude 是 glm 网关变体**(model glm-5.2,open.bigmodel.cn)。`-p` 无 AskUserQuestion;`sdk_mcp_servers`(in-process MCP)在该变体未注册——外接 `.mcp.json` 才行(已证)。
- **网关 529 间歇**:open.bigmodel.cn「访问量过大」。claude boot(banner)不需网关;但处理 prompt 时 529 会报错。验证注入只需看 claude **开始处理**(读文件/调工具),不需等它收敛。
- **PTY 会话堆积**:多次 spawn 留 stale PTY。验证前 `DELETE /api/pty/<key>` 清掉。
- **后台任务被 harness 回收**:claude code 的后台 bash(serve/vite/长循环)常被 10min/随机杀。所以 serve 走用户的 `.bat`(持久);live E2E 用 **cron**(session-only,跨 turn 存活,但关窗口就停)。
- **design prompt 里仍写「调 mcp__lifecycle__clarify」**——交互终端的 claude 没 MCP(交互 spawn 没传 --mcp-config),读这行会报「无此工具」。**次要问题**:交互式靠人 steer,不需要 clarify。可给 `build_design_dimensions_section` 加 `interactive` 标志(交互时改「在终端问人」),但先把注入搞定。

## 8. test story

- `manual-emergency2`(借款增加第二紧急联系人,workspace=D:\hc-all,profile=minimal,
  current_stage=design)。PRD + prompt 文件已就绪。用它验证注入。

## 9. memory(本机)

`~/.claude/projects/D--hc-all/memory/story-lifecycle-design-hitl.md` 已记方向变更 + MCP 方案验证。
handoff 后应更新它标注「交互式终端方向 + 注入卡点 + bracketed-paste 待验证」。

## 10. live 验证结果(2026-07-07 22:5x)—— ✅ bracketed-paste 注入通过

**结论:`5e65535c` 的 bracketed-paste 注入 live 验证通过,且端到端跑通。** claude 收到注入的
design prompt 后**完成整个 design 阶段**:Read PRD → 出设计内容 → 写 `design.md`/`ddl.md` →
写 done 握手 `.story/done/manual-emergency2/design.json`(`{stage:design,status:done}`,含
PH 手机号正则 `^(08|09)\d{9}$`、formatValidationService 灰度、`E_PARAM_PHONE_WRONG` 等)。
证明 `\x1b[200~ 文本 \x1b[201~` + 0.4s + `\r` 在 claude Code v2.1.195(glm 网关)的 Ink TUI
里**能 submit**。若没 submit,claude 会停在 `>` idle,不可能 Read PRD 出设计更不会写 done。

**证据链(全原始 PTY 字节,WS 直读,绕开前端)**:
- claude 设终端标题 `执行 design 阶段任务`(收到 design prompt)。
- claude 调 `Read` 读 PRD;PRD 域内容(`紧急联系`/`借款`)出现在输出。
- thinking 37s/43s、6.6k→14.4k tokens,出**真实设计内容**:「后端已满足无 DDL,缺口是给
  紧急联系人 phone 补菲律宾手机号格式校验,用 formatValidationService 镜像 name 校验」,
  引用 `t_format_validation_rule`/`AGENTS.md`/`spec-template.md`。
- 162KB 输出,持续 thinking。

**验证方法(关键,绕过两个坑)**:
1. serve 必须 restart 加载 `5e65535c`(`reload=False`;查 python 进程 CreationDate >
   commit 时间 20:31)。用户跑 `启动story-serve.bat`。
2. **绕开前端**:前端终端容器 8px 宽 → FitAddon 算 2 列 → 发 resize 把 PTY 缩成 2 列 →
   claude TUI 糊成 2 字一行 + 前端 WS 抢占 `_queue`。改用 REST `POST /api/pty/<key>/spawn`
   + 自己 WS 连 `/ws/pty/<key>` 读原始字节(PTY 保持 spawn 时的 30×120,无 resize 干扰)。
   WS 客户端用 `.venv-monorepo-test` 的 python(`websockets` 16.0),输出写 UTF-8 文件再读
   (Windows gbk 控制台 print emoji/box 字符会崩)。
3. 注入走两条路都验证过:(a) `_ensure_story_agent_pty` 的 bracketed paste(server-side,
   180s fallback 触发);(b) WS 直发同样字节(client-side)。claude 都 submit 了。
4. **之前那 90 分钟「测试」是无效的**:serve restart 后内存 PTY 表空,点「启动终端」复用了
   一个 19:54 的孤儿 PTY → `reused=True` → 注入代码没跑(prompt 文件 mtime 还是 19:54)。
   教训:spawn 前先 WS 确认 "No PTY",且看 prompt 文件 mtime 是否更新到当下。

**⚠️ 新发现 bug(独立于 bracketed-paste,需单修):readiness_marker 不匹配。**
`knowledge/adapters/claude.py:16 readiness_marker = r"❯"`,但 claude v2.1.195 的 prompt 是
`>`(boot capture 实测:`> <ESC>[7m <ESC>[27m`,无 `❯`)。→ `_wait_ready` 永不匹配 → poll
满 180s → 注入只在 fallback 触发。功能能用但慢(人等 3 分钟)。修法:marker 改匹配 `>` 或
mode bar(`bypass permissions on`/`shift+tab to cycle`)。注意 curl 被 kill 后 FastAPI
handler 线程仍跑到 180s fallback 才注入(BG1 curl 死了但注入仍触发,就是这原因)。

**下一步建议**:
1. 修 `readiness_marker`(claude.py:16)→ 注入从 180s 降到 ~boot 完成时(~10s)。
2. 修前端终端容器 8px 宽的 layout bug(或交互时前端自己发 prompt 走 ws.send——§6 退路 2)。
3. design prompt 里「调 mcp__lifecycle__clarify」对交互式 claude 仍报无此工具(§7 次要);
   给 `build_design_dimensions_section` 加 `interactive` 标志改「在终端问人」。
4. 修完上面,交互终端 HITL 主路径即可用:人 watch + Esc 打断 + 打字纠偏。

## 11. 最终方案(2026-07-08)—— `claude "query"` 取代 PTY 注入

§10 的 bracketed-paste 虽证 work,但**注入时机**是死胡同:server-side 靠猜 claude 输出判
readiness——TUI ~6s 画完(prompt+mode bar),但 ~100s 才真 ready(加载 skill/MCP/索引),
这之间**没输出信号**。改 marker 为 `shift+tab`(6s 触发)→ 太早,被 claude 吞掉(180s 0
字节);保留 `❯`(永不匹配→180s fallback)能用但慢。

**正解:用 `claude "query"`(CLI 原生)把 prompt 作初始消息传入** —— claude 自己管
readiness,加载完自动处理初始 prompt(auto-submit),绕过整个注入/时机问题。人照样 watch +
Esc + steer。([CLI reference](https://code.claude.com/docs/en/cli-reference):
`claude "query"` = "Start interactive session with initial prompt"。)

**改动**:
- `claude.py`: `interactive_launch_cmd(model, prompt="")` → `["claude"] + ([prompt] if prompt else [])`。
  marker revert 回 `❯`(交互路径不再用;planner 自主路径靠 180s fallback,不动)。
- `api.py`: 抽 `_build_stage_launch_prompt(story)` helper(写完整 prompt 到文件 + 返回单行
  「读文件」指令)。**两个 spawn 端点都走它**:`/api/story/{key}/sessions/spawn`(前端「启动
  终端」调的)+ `/api/pty/{key}/spawn`(legacy)。删掉 bracketed-paste 注入 + readiness 等待。
- `base.py`: `interactive_launch_cmd` 加 `prompt` 参。
- 前端 `TerminalPanel.tsx`: 加 ResizeObserver(container resize 时 refit)——治「tab 后台
  mount 时 0-width → fit 2 列 → PTY 被 resize 成 2 列」(8px 是后台 tab artifact,前台 1414px
  正常,但 xterm 不 refit 就卡在初始 2 列)。已 `npm run build`。
- 测试:53 passed(test_pty_ready / test_api_integration / test_execution_mode)。

**端到端验证(2026-07-08 00:15)**:直接调 endpoint 1(`sessions/spawn`)→ claude.cmd 带
prompt arg 起 → claude 自动开跑 design(WS 抓 190KB:thinking + Read PRD + 紧急联系/借款域
内容)→ 写 `design.json`(stage=design, status=done)。**无 PTY 注入、无 marker 猜测、无
180s 等待**。

**踩过的坑(别重蹈)**:
- 前端「启动终端」先调 endpoint 1(`sessions/spawn`,原 generic 无 prompt)→ 起 blank claude;
  只在 endpoint 1 失败时 fallback 到 endpoint 2。所以**必须让 endpoint 1 也 seed prompt**
  (不能只改 endpoint 2)。
- serve `reload=False`,改后端要 restart serve;前端改要 `npm run build`。
- 读 PTY 输出别走前端(2-col + 抢 queue),用 WS 直连 `.venv-monorepo-test` python
  (`websockets` 16.0),输出写 UTF-8 文件再 Read(gbk 控制台 print emoji 会崩)。
- `claude "query"` 的 prompt 是单行「读文件」指令(短,作 arg);完整 7099 字 prompt 在文件
  里让 claude Read(避免超长命令行)。

**后续(2026-07-08):交互式 clarify 协议。** 交互式 claude 没 MCP,design prompt 原「调
`mcp__lifecycle__clarify`」会报无此工具。给 `build_design_dimensions_section(*, interactive=False)`
加旗标:interactive=True 时改「在终端直接问人」(人 watch 终端直接答);`_build_cli_prompt(interactive=...)`
串到 `_build_interactive_stage_prompt`(传 True)。自主路径默认 False 保留 MCP。测试 57 passed。
