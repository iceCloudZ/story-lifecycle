# 程序化驱动 claude Code:从交互式 TUI 注入到会话持久化

> 一个 agent orchestrator 踩进 coding-agent 底层机制的深度记录。
> **自包含**:不依赖对话上下文,读者只需懂 Python / 前端基础。
> **用途**:后续学习 / 面试材料 / 给 AI 评审。
> 仓库:`D:\github\story-lifecycle`(story-lifecycle orchestrator)。涉及 claude Code v2.1.195(glm 网关变体)。
> 起点 2026-07-07。

---

## 0. TL;DR(30 秒版)

我做了 **`story-lifecycle`**——一个把业务 story(TAPD 需求)自动跑过 `plan → design → build → verify` 阶段的 agent orchestrator。它的 **design 阶段需要 HITL(human-in-the-loop)**:claude **交互式**跑,人实时 watch + Esc 打断 + 打字纠偏(省 token、保方向)。

为了实现这个,我被迫钻进 **claude Code 的底层机制**——这是大多数"会用 API / SDK"的人不会碰的层:

- **PTY + Ink TUI 注入**:怎么把 prompt 喂进 claude 的交互式终端(bracketed paste、`claude "query"` 初始消息语义、readiness 时机)。
- **会话持久化**:`--session-id` / `--resume`、transcript 存储格式(`~/.claude/projects/<project>/<uuid>.jsonl`)、cwd-scoped 查找,用**确定性 `uuid5`** 把 claude session 和业务 story 焊起来。
- **HITL vs 自主** 的架构权衡、MCP clarify(外接 vs in-process)、done 握手协议。
- 生态机制:hooks、Agent SDK、`--bg`+daemon、Agent View 的取舍。

核心难点一句话:**claude Code 的交互式 TUI 是给人用的,不是给程序驱动的——把它变成可编排、可持久化、可 resume 的 agent,得反向工程它的内部语义。**

---

## 1. 背景(自包含)

### 1.1 story-lifecycle 是什么

一个 **AI agent 编排器(orchestrator)**,把一个软件需求(story)自动推进过多个工程阶段:

```
TAPD 需求 ──→ [plan] ──→ [design] ──→ [build] ──→ [verify] ──→ 交付
                ↑ autonomous    ↑ HITL(本文焦点)    ↑ autonomous
```

- 每个阶段由一个 **adapter**(claude / codex / kimi)跑;adapter 在 PTY 里起一个 coding agent。
- **done 握手协议**:每个阶段 claude 跑完写一个 `.story/done/<key>/<stage>.json`(`{stage, status:"done", summary, files_changed}`),orchestrator 检测到就推进下一阶段。
- 技术栈:FastAPI serve(`127.0.0.1:8180`)+ React 前端 + WebSocket 终端(xterm.js)+ winpty PTY 层。

### 1.2 为什么 design 阶段要 HITL

| 模式 | 跑法 | 优点 | 缺点 |
|---|---|---|---|
| **自主 headless**(`claude -p`) | claude 自己跑完,人不在场 | 全自动、可批量 | 方向跑偏浪费大量 token;中途无法纠偏 |
| **交互式 HITL**(本文) | claude 交互式跑,人 watch + Esc 打断 + 打字纠偏 | 省 token(早纠偏)、保方向、人掌控 | 需要解决"怎么程序化启动 + 让人接管" |

design 阶段(把需求转成技术方案)歧义多、资方差异大,**值得人在环里逐问澄清**。自主跑容易发散/context rot。所以 design 走交互式 HITL;build/verify 走自主。

### 1.3 claude Code 是什么 + 我们用的变体

**claude Code** 是 Anthropic 的 coding agent CLI(`claude` 命令),默认开一个 Ink(React for CLI)写的交互式 TUI,能读写文件、跑命令、调 MCP 工具。

**我们用的是 glm 网关变体**(`v2.1.195`,model `glm-5.2`,网关 `open.bigmodel.cn`)——基于 claude Code 但接 glm 模型。变体有限制(实测):
- `claude -p`(headless)**无 AskUserQuestion**(不能交互提问)。
- `sdk_mcp_servers`(in-process MCP)在该变体**未注册**——只能用外接 `.mcp.json`。
- 其余 flag(`--session-id`、`--resume`、`--name`、`-p`、`--bg` 等)基本与官方一致(`claude --help` 可见)。

> ⚠️ 这点很关键:**官方文档(code.claude.com)描述的是官方 claude Code;变体的行为必须实测**。本文所有结论都标了"已验/待验"。

### 1.4 整体架构

```
浏览器 (xterm.js TerminalPanel)
   │ WebSocket onData↔onmessage
   ▼
FastAPI serve (8180)
   ├─ POST /api/story/{key}/sessions/spawn  ──┐
   ├─ POST /api/pty/{key}/spawn (legacy)    ──┤  → _build_stage_launch_cmd → spawn PTY
   └─ WS   /ws/pty/{key}  ──────────────────┐
   ▼                                        │
ManagedPty (winpty)                         │
   └─ claude.cmd → node claude.js (--session-id/--resume "<prompt>")  ← cwd=workspace
```

- 前端 xterm 双向:`onData → ws.send → pty.stdin`;`pty.stdout → ws.send → term.write`。
- PTY 用 winpty(Windows 真 PTY,claude 才会起 TUI;纯 subprocess pipes 会让 claude 走非交互)。

---

## 2. 底层机制深挖(别人很少碰的层)

### 2.1 PTY + Ink TUI 注入:把 prompt 喂进交互式终端

**问题**:spawn 起 claude 后,它停在空白输入框(`❯`/`>`)等人打字。怎么**自动**把 design prompt 喂进去并让它开始跑?

**为什么难**:claude Code 用 **Ink**(React for CLI),输入框是 `ink-text-input`。它把**裸 PTY 写入当 paste,不触发 submit**(claude-code issue #15553)。即你 `pty.write(prompt + "\r")`,文字进了输入框但**不提交**,claude 干等。

**演进(四步,每步都有为什么不够)**:

| 尝试 | 做法 | 结果 | 为什么不够 |
|---|---|---|---|
| ① 侧文件协议 | claude `-p` 写 `clarify_request.json` 后停 | ❌ 推翻 | `-p` 无 AskUserQuestion + 自主跑浪费 token |
| ② 外接 MCP clarify | headless `-p` + 外接 MCP,claude 调 `mcp__lifecycle__clarify` 阻塞等人答 | ✅ live PASS | 仍是自主模式,人不能 watch/interrupt/steer |
| ③ bracketed paste | 交互式 `["claude"]`,spawn 后注入 `\x1b[200~ 文本 \x1b[201~` + `\r` | ✅ live PASS(机制对) | 注入**时机**靠猜 readiness,不可靠(见下) |
| ④ `claude "query"` | `claude "<prompt>"` 作**初始消息**,auto-submit | ✅✅ 端到端 PASS | 当前正解 |

**bracketed paste 机制**(标准终端协议):终端启用 bracketed paste 模式后,paste 内容用 `\x1b[200~` … `\x1b[201~` 包裹,应用据此区分"粘贴"vs"键盘"。claude 的 Ink text-input 收到 bracketed paste 会把整段干净填进输入框,再单独 `\r`(keystroke)提交。

**`claude "query"`**(CLI 原生):`claude "<prompt>"` 开交互式 TUI 时把 prompt 作**第一个 user message**,auto-submit。claude 自己管 readiness(加载 skill/MCP 完自动处理初始 prompt),**绕过了"什么时候注入"这个时机问题**。

#### readiness 时机:为什么靠猜输出是死胡同

`_wait_ready` 的设计:poll PTY 输出直到一个 marker(如 prompt 符)出现再注入。问题:

- claude **~6s** 画完 TUI(banner + `>` prompt + mode bar `shift+tab to cycle`)。
- 但 **~100s** 才**真正 ready**(加载 skill / MCP / 索引 hc-all 大仓库)。
- 这之间**无输出信号**——TUI 画完 ≠ 可接受输入。

实测:marker 用 `shift+tab`(6s 出现)→ 注入太早 → **被 claude 吞掉**(180s 0 字节)。marker 用 `❯`(老版本 prompt 符,v2.1.195 实际是 `>`)→ 永不匹配 → 等 180s timeout fallback → 慢但能用。

**结论**:靠猜 claude 输出判 readiness 根本猜不对。`claude "query"` 让 claude **自己**管 readiness,整个问题消失。

**验证状态**:
- ✅ bracketed paste 注入机制 live PASS(claude ready 时能 submit,实测 claude Read PRD + 出设计 + 写 `design.json`)。
- ✅ `claude "query"` 端到端 PASS(直接调 spawn 端点 → claude 自动跑 design → `design.json status=done`)。

### 2.2 会话持久化:杀进程/重启不丢进度

> ✅ **实测确认(2026-07-08,用户手动验):方案 A 在本变体可行。之前"no-op / broken"全是 force-kill 测试假象。**
> - `--session-id <uuid>` **honored**:干净退出后 transcript 落在 `~/.claude/projects/D--hc-all/<uuid>.jsonl`,对话全在。
> - `--resume <uuid>` + `--resume <name>` **都 work**:resume 后 claude 记住之前对话(口令验证通过)。
> - **根因(方法论教训)**:transcript 在**干净退出(`/exit`)时 flush**。之前子代理 + 我的程序化测试全是 **force-kill**(`pty.kill()` / 超时杀)→ 截断 flush → 没 transcript → resume "No conversation found"。**force-kill 制造了假象**。用户在新窗口手动跑(交互式 + `/exit` 干净退出)才验出真相。
> - **对 design HITL**:real design 会话(大体量、跑很久)transcript 会持续写,即便 serve 重启 force-kill 也保留大部分(参考 D--hc-all 里 `c594e264` 427KB 等);下次 `claude --resume <uuid>` 加载接着干。为最佳 resume 状态,孤儿清理优先尝试干净退出(`/exit` via bracketed paste),force-kill 兜底。
> - **教训(深度证据)**:程序化验 agent 内部机制,**测试条件必须含干净退出**(让 agent 自己 flush),否则 force-kill 制造假象 → 错误结论。这本身就是 agent infra 的坑——本文档曾一度被这个假象误导,后经手动验纠正。

**问题**:serve 重启(或杀孤儿 claude)→ 进度全丢?要能 resume。

**关键发现:claude 自己就在存进度**。每个 session 的 transcript 持续写:

```
~/.claude/projects/<project>/<session-id>.jsonl
```

- `<project>` = cwd 路径里**非字母数字字符替换成 `-`**。例:`D:\hc-all` → `D--hc-all`。
- 每行一个 JSON(message / tool use / metadata)。**杀进程不丢**(已落盘)。

**两个 flag**(`claude --help` 确认有,变体支持):
- `--session-id <uuid>`:指定 session ID(必须合法 UUID)——**新会话用我们给的 ID**。
- `--resume <uuid>`:resume 该 session(加载 transcript 接着干)。**查找是 cwd-scoped**——必须在原 session 的同一目录跑,否则 `No conversation found`。

#### 把 claude session 和业务 story 焊起来(核心抽象)

问题:新窗口(新 serve)怎么知道旧会话的 session ID?

**答:UUID 不是 claude 藏起来的——`--session-id` 让我们塞给它。我们用确定性派生:**

```python
session_id = uuid5(NAMESPACE_DNS, f"{story_key}:{stage}")
```

- 同一 `story_key + stage` → **永远同一 UUID**(uuid5 确定性)。
- `story_key`/`stage` 在 story DB(磁盘),serve 重启不丢 → 新 serve 重算即得同一 UUID。
- transcript 在 `~/.claude/projects/<project>/<uuid>.jsonl`(claude 存,serve 重启不丢)。
- 标记文件 `.story/context/<key>/session_<stage>.json`(我们存,确认"起过这个 session")。

→ **三处持久化全不在 serve 内存**:UUID(重算)+ 标记(磁盘)+ transcript(claude 磁盘)。新 serve 自给自足。

#### NEW vs RESUME 判定

```python
def _build_stage_launch_cmd(story, adapter, model) -> (cmd, is_resume):
    session_id = uuid5(NAMESPACE_DNS, f"{story_key}:{stage}")
    if marker_file.exists():
        return claude --resume <uuid> "继续上次的任务...", True   # RESUME
    seed = _build_stage_launch_prompt(story)  # 写完整 prompt 文件 + 返回单行读文件指令
    write(marker_file)
    return claude --session-id <uuid> --name <key>-<stage> "<seed>", False  # NEW
```

#### 方案 A(--resume)vs 方案 B(--bg + attach + daemon)

| | 方案 A:`--session-id`+`--resume` | 方案 B:`--bg`+`attach`+daemon |
|---|---|---|
| 模型 | 杀进程,transcript 落盘,下次 `--resume` reload | session 一直活着(daemon 托管),`attach` 接上 |
| 孤儿 | 有(杀不干净时)→ 需清理 | 无(daemon 管) |
| 续接 | reload transcript(长 transcript 可能慢/claude 提议 summary resume) | live 续接(无 reload) |
| 复杂度 | 低(标准 flag) | 高(daemon、background 模型、attach 语义) |
| 变体支持 | `--session-id`/`--resume` 在 `--help` ✅ | `--bg` 在 `--help`,daemon/attach 待验 |
| 取舍 | **先上**(满足需求) | 留后续(若长会话 reload 慢 / 想彻底无孤儿) |

**验证状态**(2026-07-08,用户手动验确认可行):
- ✅ `--session-id <uuid>` **honored**(干净退出后 transcript 在 `<uuid>.jsonl`,对话全在)。
- ✅ `--resume <uuid>` + `--resume <name>` **都 work**(resume 后记住口令/对话)。
- ⚠️ transcript 在**干净退出(`/exit`)时 flush**;force-kill(serve 重启)对**短/小**会话可能没 flush,但 **real 长 design 会话持续写**(保留大部分,参考 `c594e264` 427KB)。
- ⇒ **方案 A 在 glm 变体 v2.1.195 可行**。代码保留。
- (历程:程序化 force-kill 测试曾给出假阴性"no-op/broken",经用户手动干净退出测试纠正——见上方方法论教训。)

### 2.3 HITL 架构:交互 vs 自主、澄清、握手

**交互式 vs 自主**的权衡见 §1.2。关键是**同一个 adapter(claude)两种跑法**:
- 自主:`claude -p "<prompt>"`(headless,可接 MCP,orchestrator 全程控)。
- 交互:`claude "<prompt>"`(TUI,人 steer,无 MCP——见下)。

**MCP clarify(逐问澄清)**:design 遇关键岔路时问人。
- 自主路径:外接 `.mcp.json` 加载 `mcp__lifecycle__clarify` 工具,claude 调它阻塞等人答(in-process `sdk_mcp_servers` 在变体未注册,只能外接)。
- 交互路径:claude 没 MCP(交互 spawn 没传 `--mcp-config`)→ 改「**在终端直接问人**」(prompt 里加 `interactive` 旗标,`build_design_dimensions_section(interactive=True)`)。

**done 握手**:prompt 里写明完成协议(写哪个 done 文件、字段),claude 跑完自写 → orchestrator 推进。这让交互式 claude(人 steer)和自主 claude 共用同一套阶段推进逻辑。

### 2.4 生态机制(研究过取舍,暂未用)

- **hooks**(PreToolUse / SessionEnd 等):可拦截工具调用、session 结束归档 transcript。适合做权限门、审计。
- **Agent SDK**(Python/TS):程序化跑 claude,收每条 message。适合 headless 自动化(但失交互 steer)。
- **`--bg` + daemon + Agent View**:background session,daemon 独立托管,`attach` 接上。方案 B 的基础。
- **checkpointing / `--fork-session`**:rewind、分支试错。

---

## 3. 关键决策 + 取舍(面试可讲的叙事)

> 面试官不会看 handoff doc。你得能 30 秒讲清"做了啥、难点、调研了 A/B/C、选 B 因为…、踩了 D 坑、用 E 解决"。下面是凝练版。

**叙事**:"我做了一个 agent orchestrator,把软件需求自动跑过 design/build/verify。design 阶段要人在环(HITL)省 token。难点是**程序化驱动 claude Code 的交互式 TUI**——它的输入框把程序写入当 paste 不提交(官方 issue #15553)。我调研了四条路(侧文件、MCP、bracketed paste、`claude "query"`),最后用 `claude "query"` 把 prompt 作初始消息,让 claude 自管 readiness,绕开了注入时机问题。又用确定性 `uuid5` + `--session-id`/`--resume` 做了会话持久化,杀进程重启可 resume。"

**四个决策点**(都能展开讲):
1. **注入方式**:四步演进,每步为什么不够(见 §2.1 表)。
2. **readiness**:为什么靠猜输出是死胡同(TUI 画完 ≠ ready,无信号)→ 让 claude 自管。
3. **持久化**:方案 A vs B,为什么先 A(简单可靠满足需求)。
4. **孤儿进程**:Job Object 为什么不可靠(见 §4)→ 清理策略。

---

## 4. 踩坑实录(深度证据)

每个坑都是"现象 → 根因 → 验证 → 解决"的完整链,是深度的硬证据。

| 坑 | 现象 | 根因 | 验证 | 解决 |
|---|---|---|---|---|
| **孤儿 PTY 复用** | 点启动终端,claude 不开跑(prompt 文件 mtime 没更新) | serve 重启后内存 PTY 表空,spawn 复用了旧 serve 留的孤儿 PTY → `reused=True` 跳过注入 | 查 prompt 文件 mtime(还是旧的) | spawn 前先 WS 确认 "No PTY" |
| **serve `reload=False`** | 改了后端代码不生效 | uvicorn `reload=False`,改 `api.py` 不重载 | 查 serve 进程 CreationDate vs commit 时间 | 用户跑 bat 重启 serve |
| **前端终端 8px 容器** | 终端字 2 列一行,claude TUI 糊 | 后台 tab 0-width → FitAddon 算 2 列 → 前端发 resize 把 PTY 缩成 2 列;且前端 WS 抢占 queue | webbridge 量 container 宽度(8px)+ ancestor walk(前台 1414px 正常) | 加 ResizeObserver(container resize 时 refit)+ 读 PTY 走 WS 直连绕开前端 |
| **readiness marker `❯` 不匹配** | 注入等满 180s 才触发 | v2.1.195 的 prompt 是 `>` 不是 `❯` | WS 抓 boot 字节,grep `❯`(无) | 已不需要(claude "query" 自管 readiness) |
| **Job Object 漏杀** | 重启后孤儿 claude 还在 | `claude.cmd → node`,KILL_ON_JOB_CLOSE 连带杀孙进程有时漏 | `tasklist` 看 claude.exe 跨 restart 存活 | 待加:bat 重启前 `DELETE /api/pty` + serve 启动扫孤儿 |
| **Windows gbk 控制台** | Python print emoji/box char 崩 | 控制台 gbk 编码 encode 不了 `⏵`(U+23F5)等 | `UnicodeEncodeError: 'gbk' codec` | 脚本输出写 UTF-8 文件再 Read,不 print 到控制台 |
| **webbridge 中文 inline** | curl 传中文变 `?` | Windows shell corrupts non-ASCII inline | — | curl.exe `--data-binary @file`(file-body) |

---

## 5. 面试 Q&A(经得起追问)

- **Q: 为什么 `uuid5` 不是 `uuid4`?**
  A: uuid4 随机,得存表才知道哪个 story 对哪个 session;uuid5 确定性派生,`story_key:stage` → 同一 UUID,不存表也能重算,新 serve 自给自足。代价:uuid5 单向(反推不回 story),但我们永远 story→uuid 方向,不需要反推。

- **Q: transcript 格式变了怎么办?**
  A: 官方明说 "entry format is internal, changes between versions, scripts that parse directly can break"。所以我们**不解析 transcript**——只用它存在性判断"有没有这个 session",读内容走 `/export` 或 `claude -p --resume` 的结构化输出。

- **Q: `--bg` 为啥不选?**
  A: 它是 background agent 模型(daemon 托管、attach 接),改了整个 spawn 流程;且有持久化 gap(issue #68146:无 terminal 时 daemon 不一定活着)。当前需求(杀掉、下次 resume)用 `--resume` 够了,简单可靠。长会话 reload 慢或想彻底无孤儿时再切。

- **Q: mid-tool-call 被杀,resume 会怎样?**
  A: transcript 按完成 turn 落盘。mid-tool-call 的中间态可能没存,resume 从最近干净 turn 接(可能重做最后一小步)。可接受。真要严格可重启 `--fork-session` 分支试。

- **Q: bracketed paste vs `claude "query"` 哪个好?**
  A: `claude "query"`。bracketed paste 是"程序往 TUI 塞输入",得自己判 readiness(死胡同);`claude "query"` 是"用 CLI 原生初始消息",claude 自管 readiness。前者是 hack,后者是正道。

- **Q: 为什么不直接用 Agent SDK?**
  A: Agent SDK 是 headless(收 message 流),失掉"人 watch + Esc 打断 + 打字纠偏"的交互 steer。design HITL 要的就是交互。SDK 留给自主场景。

- **Q: 两个 spawn 端点为什么都要改?**
  A: 前端"启动终端"先调 `sessions/spawn`(endpoint 1,原 generic 无 prompt),失败才 fallback `pty/spawn`(endpoint 2)。只改 endpoint 2 的话,endpoint 1 成功就起 blank claude,prompt 没注入。所以**两个都得走 seed 逻辑**(抽 helper `_build_stage_launch_cmd`)。

---

## 6. 评估:这是核心竞争力吗

**诚实判断:现在是"会用 + 踩得深",离"核心竞争力"还有距离,但方向对,且踩到了别人很少碰的层。**

### 碰到的层级(别人很少到)

大部分用 claude Code 的人停在"写 prompt / 用 SDK 跑 `-p`"。这个项目真碰了底层:
- **PTY + Ink TUI 注入**:bracketed paste、`claude "query"` 初始消息语义、readiness 时机。程序化驱动交互式 agent 的硬骨头,文档基本没有,全靠实测 + 翻 issue。
- **会话持久化**:`--session-id`/`--resume`、transcript 存储格式、cwd-scoped 查找。用确定性 uuid5 把 claude session 和业务 story 焊起来——这个抽象很多人想不到。
- **HITL 架构**:人 steer vs 自主 headless 的权衡、MCP clarify(外接 vs in-process)、done 握手。
- **生态机制**:hooks、Agent SDK、`--bg`+daemon、Agent View 都研究过取舍。

→ 招 **"AI infra / agent orchestration"** 方向(做 agent 平台、coding agent、IDE 集成的公司)的人,能聊到这个深度的不多。**这是强加分项**,能让你从"会用 API"的人里跳出来。

### 差距(怎么变成真竞争力)

1. **没沉淀成可讲的叙事**:面试官不看 handoff doc。要能 30 秒讲清(本文 §3 是凝练版,得复习成肌肉记忆)。
2. **深度要经得起追问**:本文 §5 的 Q&A 得答得上(其实答得上,但要练)。
3. **缺"我造过/贡献过"的标签**:claude Code 闭源,我们是用它。真正的护城河——**开源一个类似 orchestrator,或写篇深度 blog**("如何程序化驱动 claude Code 的交互式 TUI + 会话持久化")。这个 niche 现在几乎没人写,写了就是第一梯队。

---

## 7. 后续学习路径 + 可公开资产

### 待深挖(学习)
- **Agent SDK** 内部:message 流、tool use 协议、怎么嵌进 TS/Python app。
- **hooks 系统**:PreToolUse/SessionStart/SessionEnd 生命周期,能做什么(权限门、审计、transcript 归档)。
- **`--bg` + daemon 内部**:supervisor 怎么管 worker、attach 协议、持久化 gap(#68146)。
- **transcript JSONL 格式**:虽然官方说 internal,但逆向后能做更细的进度追踪/回放。
- **checkpointing**:rewind 代码 + 对话的实现(git?内部快照?)。

### 可公开资产(护城河)
- **开源**:`story-lifecycle` 的 design HITL 部分(交互式终端 + 会话持久化)抽成独立 lib——"programmatic claude Code session driver"。
- **深度 blog**:本文的 §2 + §3 + §4 改写成可发布的技术文章。niche 几乎空白。
- **issue/PR 贡献**:claude-code #15553(bracketed paste)、#68146(persistent daemon)等 issue 可以补实测/用例。

---

## 8. 参考资料

**官方文档**(code.claude.com):
- [CLI reference](https://code.claude.com/docs/en/cli-reference) — 所有 flag(`--session-id`、`--resume`、`--name`、`--bg`、`-p` 等)。
- [Manage sessions](https://code.claude.com/docs/en/sessions) — resume/session-id 语义、transcript 存储、cwd-scoped。
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless) — Agent SDK / `-p`。
- [Manage multiple agents with agent view](https://code.claude.com/docs/en/agent-view) — `--bg` + daemon。

**claude-code issues**(github.com/anthropics/claude-code):
- #15553 — Ink text-input 把裸 PTY 写入当 paste 不 submit。
- #68146 — background sessions 应 keep daemon alive(持久化 gap)。
- #59848 — interactive sessions 被 misclassified 为 background jobs。

**第三方参考**:
- AgentPTY(github.com/quietforgelabs/AgentPTY)— "sends prompt to interactive claude + returns response" 的实现。
- Ralph TUI — 第三方 TUI 集成 claude CLI。

**本项目内部**:
- `docs/handoff-design-hitl.md` — 完整演进时间线(§1-§11)。
- `orchestrator/service/api.py` — `_build_stage_launch_cmd` / `_ensure_story_agent_pty` / `_build_stage_launch_prompt`。
- `knowledge/adapters/claude.py` — `interactive_launch_cmd`(session-id/resume)。
- `infra/terminal/pty.py` — ManagedPty / `_wait_ready` / `kill` / Job Object。
- `frontend/src/components/TerminalPanel.tsx` — xterm + ResizeObserver。

---

*验证状态汇总:`claude "query"` 端到端 ✅、bracketed paste 机制 ✅、readiness 死胡同 ✅(已绕过)、`--session-id`/`--resume` ✅(用户手动验:honored + resume 记住对话;之前 force-kill 测试假象已纠正)→ 方案 A 可行,见 §2.2。*
