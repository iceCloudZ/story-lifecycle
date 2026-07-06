# Design HITL 全链路落地 · 执行手册（自包含）

> **目标**：把 design 阶段从「一次性产出 decision_points」（a34cc843，已落地）升级为「**claude 逐问 AskUserQuestion + 人答 HITL**」——动态澄清（第一个答影响后续问），替代扁平决策点列表。
> **自包含**：新会话读完本文件即可一次做完，不需其他上下文。
> **前置**：#4 已由 a34cc843 解决（claude design 在 hc-all 8.2min 收敛、brainstorming 0 次、产出 design.json）。详见 `docs/real-story-e2e-runbook.md` §7.3/§7.4。
> **起点日期**：2026-07-07。

---

## 0. 背景与铁律

1. **#4 已解决**：a34cc843（`build_design_dimensions_section`：13 维度 checklist + 禁 brainstorming + security playbook 窄注入）让 claude design 收敛。子代理 claude 真跑验证：EXIT 124→0（8.2min）、design.json 产出、brainstorming 22→0、10 条 decision_points、DP5 主动识别 appVersion 降级（飞轮生效）。
2. **(a) 是交互升级**，不是修 #4：design 从「claude 一次性产出 DP」→「claude 逐问 + 人答」。动态性来自 claude 根据前答出下一问（资深工程师 brainstorming 的本质）。
3. **claude 已证会遵守 prompt 约束**（禁 brainstorming 它执行了 + 思考复述）→ 改 prompt「遇歧义 AskUserQuestion 提问」它也会遵守。
4. **铁律**：TDD（RED→GREEN→commit）、**不动 `pty.py`**（动前重读 `tests/test_pty_tap.py`）、每步 commit 末尾 `Co-Authored-By: Claude <noreply@anthropic.com>`、claude 本机全 allow、Decider 纯函数（LLM/DB/PTY 全注入）。

---

## 1. 环境（均已就绪）

| 项 | 值 |
|---|---|
| orchestrator 仓库 | `D:\github\story-lifecycle`（默认分支 main，本地 commit；push 受沙箱阻断，不用 push） |
| 目标工作区 | `D:\hc-all`（多 repo） |
| 验证 story | `tapd-1144381896001065570`（联系人姓名校验；PRD `D:/hc-all/story/1065570-联系人姓名校验/PRD.md`） |
| story serve | `127.0.0.1:8180`；健康 `GET /api/session/health` |
| deepseek key | `~/.story-lifecycle/config.yaml`（model deepseek-v4-pro）。代码经 `load_config_to_env()` 读 |
| CLI | `claude`（可用，本机全 allow）；`kimi`（-p headless 可用） |
| pytest | `./.venv-monorepo-test/Scripts/python.exe -m pytest packages/story-lifecycle/tests/`（**别用项目 .venv**） |
| LLM 调用 | `from story_lifecycle.infra import llm_client; llm_client.get_llm().invoke(prompt, system=...)` |
| 前端 | `packages/story-lifecycle/frontend/`（React 19 + Vite + react-query + zustand + xterm）；`npm run dev` |

---

## 2. 现状关键代码（改前必读 + 核实 line）

- **design prompt 生成**：`orchestrator/engine/planner.py::_build_cli_prompt`（~line 975）+ `orchestrator/engine/prompt_sections.py::build_design_dimensions_section`（a34cc843 加；现文案「**不要**调用 brainstorming、**不要**提澄清问题」+ 13 维度 checklist + security playbook 注入）。`_build_cli_prompt` 在 design stage 注入 `dimensions_section`（~line 1015）。
- **supervisor（层1）**：`orchestrator/engine/supervisor.py`
  - `decide_response(prompt, ..., llm_invoke)`（~line 27）：现 `raw = llm_invoke(prompt)` **自动答**（deepseek）。**(a) 改 HITL 的核心**。
  - `handle_pty_output(...)`（~line 84）：buffer 命中 awaiting → decide_response + 应答 + log。注释「PTY 轨(codex/kimi)」「agent-yes 三层 pattern」。
  - `supervise_pty_session`（~line 147）：PTY 轨 supervisor（daemon 线程跑）。
- **claude stream 轨**：`orchestrator/engine/claude_stream.py::supervise_claude_stream`（headless stream-json，解 permission_request/elicitation → decide_response）。改前读它看 AskUserQuestion 怎么解。
- **supervisor 接线**：`planner.py::continue_orchestrator_agent`
  - PTY 轨：~line 710 daemon `supervise_pty_session`
  - headless 轨：stdout 分支（supervise_claude_stream 接 headless_launch_cmd 那条，~line 589）
- **提问检测**：`awaiting_detector.py`（readyPatterns/enterPatterns/fatalPatterns，现 codex/kimi pattern；claude AskUserQuestion 走 stream-json tool_use，不经此）
- **前端**：`packages/story-lifecycle/frontend/src/`
  - `pages/StoryDetailPage.tsx`：详情页 + **plan confirm SSE**（line ~97 `new EventSource(planApi.streamUrl(storyKey))`，planning 时流式推 action）—— clarify 对话流直接参考这套
  - `components/OverviewTab.tsx`：plan 确认 UI（ActionCard + 确认按钮）
  - `api/client.ts`：`planApi`/`storyApi`/`apiAction`；`planApi.streamUrl`/`get`/`confirm`/`regenerate`
  - `hooks/useWebSocket.ts`、`hooks/usePTYSessions.ts`：已有实时通道
- **design.json**：`.story/done/<key>/design.json`；`planner.py:843 robust_json_parse` 读 → `log_event("completed", done_data)`
- **realtest profile**：`entry/profiles/realtest.yaml`（现 `execution_mode: interactive_pty`，design/verify cli=claude）。**(a) 块3 可能改 headless**

---

## 3. 全链路（4 块协同，不能单块）

```
claude design 遇歧义 → AskUserQuestion tool_use（一次一个）
  → supervise_claude_stream 解出 tool_use                       [块3 检测]
  → decide_response HITL：不自动答，改
       暂停 story status=awaiting-clarify
       log_event("clarification_request", {id, header, options})
       SSE/WS 推前端                                              [块2 接住·核心]
  → 前端 ClarifyDialog 展示(header/options) → 用户答            [块4 前端]
  → POST /api/story/<key>/clarify/answer {id, answer}
  → supervisor 回注 stream(claude -p stdin 或重 spawn 带答)     [块2 回注]
  → claude 基于答继续(出下一问，或收敛写 design.json)           [动态：前答影响后问]
```

**为何不能单块**：headless 下 claude 提问无人答 = #4 的卡。必须块2（supervisor 接住 + HITL 答）配合块1（prompt 提问）。

---

## 4. 块1：design prompt 改「逐问」（TDD）

- **改**：`build_design_dimensions_section` 文案从「不要提澄清问题」→「**遇关键歧义（多种选择/缺失/资方差异）用 `AskUserQuestion` 工具提问，一次一个，等用户答再继续；基于前答决定下一问；无歧义直接产出 design.json**」。
- **保留**：13 维度 checklist + 禁 brainstorming 自由探索 + security playbook 窄注入（这些让 claude 收敛，#4 教训）。
- **TDD**：`test_build_cli_prompt.py::TestDesignDimensions` 加：design prompt 含「AskUserQuestion」引导（不再含「不要提问」）。
- **注意**：headless 提问必须配合块2（接住答），否则 claude 卡。本块单独验用 deepseek mock（不真跑 claude）。

## 5. 块2：supervisor HITL（核心，TDD）

- **改** `decide_response`：加 `hitl: bool` 参数（或 env `STORY_HITL=1`），HITL 模式下检测到提问 → **不调 `llm_invoke`**，改：
  1. 暂停 story：`db.update_story(key, status="awaiting-clarify")`
  2. 落事件：`log_event(key, stage, "clarification_request", {id, header, options, ai_suggestion})`
  3. 推前端：经 SSE/WS（复用 plan stream 通道）
  4. 等答：`POST /clarify/answer` → 取 answer
  5. 回注：把 answer 喂回 claude（headless：重 spawn 带答 stdin，或 stream 注入；PTY：write PTY）→ claude 继续
- **Decider 纯函数**：`decide_response` 的「检测到提问 → 返回 hitl_action{pause, push}」保持纯函数（DB/SSE/回注注入 handler），可测。
- **TDD**：`test_supervisor.py` 加：mock 提问 buffer + hitl=True → 断言返回 `{pause, clarification_request}` 而**不调** llm_invoke。

## 6. 块3：提问检测（走 headless stream-json）

- **选 headless**（`claude -p --output-format stream-json --verbose`）：AskUserQuestion 是 `tool_use` 事件，`supervise_claude_stream` 干净解（比 PTY 加 awaiting pattern 简单）。
- **改** `realtest.yaml`：`execution_mode: headless`（现 interactive_pty）；确认 claude adapter `headless_launch_cmd` 带 `--output-format stream-json`。
- **核实**：读 `claude_stream.py::supervise_claude_stream` 看 elicitation/AskUserQuestion tool_use 怎么解 → 调 decide_response。
- **坑**：headless stdin 关（不回写）——块2 回注要用「重 spawn 带答」或 claude `--input-format stream-json` 接续答（核实 claude CLI 能力）。

## 7. 块4：前端 ClarifyDialog（对话流）

- **新组件** `components/ClarifyDialog.tsx`：接 SSE/WS 推的 `clarification_request` → 展示 AskUserQuestion（header/options/ai_suggestion）→ 用户答（选选项/自由输入）→ POST `/clarify/answer` → 接下一问（循环，动态）。
- **复用** `StoryDetailPage.tsx` 的 SSE 模式（`new EventSource(...)` + onmessage 解 type）+ react-query。
- **挂载**：StoryDetailPage 在 `status==="awaiting-clarify"` 时展示 ClarifyDialog（像 planning 时展示 plan）。
- **进度**：可选展示已答历史（对话流）。

## 8. 后端 API + 状态

- **design 子状态**：`investigating`（claude 调研/提问中）→ `awaiting-clarify`（每轮暂停）→ 回注后回 investigating → ... → `resolved`（收敛写 design.json）→ build
- **SSE**：`GET /api/story/<key>/clarify/stream`（推 clarification_request，复用 plan stream 实现）
- **回注**：`POST /api/story/<key>/clarify/answer` body `{id, answer}` → 唤醒 supervisor 回注 claude
- **改前读** `service/api.py` 看 plan stream SSE 端点（`/plan/stream` 之类）的实现，clarify 复用。

---

## 9. 验收（自验证，缺一不可）

1. **claude 提问**：design 阶段 `AskUserQuestion` tool_use >0（vs a34cc843 的 0）。
2. **supervisor 接住**：检测到提问 → `status=awaiting-clarify` + `clarification_request` 入 event_log + SSE 推前端。
3. **前端展示 + 答**：ClarifyDialog 显示问题 → 用户答 → POST answer。
4. **回注 + 继续**：answer 回注 claude → claude 基于答继续（出下一问 或 收敛 design.json）。
5. **动态**：第一个答改变后续问（如答「存 hc_user」后续问本地调用；答「存 hc_config」后续问 Feign 缓存）—— 截图/日志证。
6. **收敛**：最终 design.json 产出（不卡在提问循环；最多 N 轮后收敛）。
7. **回归**：`pytest packages/story-lifecycle/tests/` 800+ passed（1 预存在 profile fail 允许）。
8. **铁律**：TDD、不动 pty.py、commit 带 Co-Authored-By。

---

## 10. MVP 切分（建议执行顺序）

1. **块2 supervisor HITL（Decider + 测）**：不依赖 claude，先做。decide_response 加 hitl 模式 + TDD。
2. **块1 prompt 逐问**：build_design_dimensions_section 改文案 + TDD。
3. **块3 headless 检测**：realtest → headless + 核实 supervise_claude_stream 解 AskUserQuestion。
4. **后端闭环验**：deepseek mock 用户答，验 claude 提问 → supervisor 接住 → 暂停 → 回注 → 继续（不真跑前端）。
5. **块4 前端 ClarifyDialog**：接 SSE + POST answer。
6. **全链路真跑**：claude design 1065570，人答，验动态 + 收敛。

---

## 11. 已知坑

- **headless 提问无人答卡死** → 必须块2 配合（接住 + HITL 答）。
- **回注机制**：headless stdin 关，回注要「重 spawn 带答」或 stream 接续（核实 claude CLI `--input-format`）。
- **claude 重环境收敛**：#4 已解决（一次性模式），但**提问模式要重验收敛**（提问-答循环是否在 N 轮收敛，不无限问）。
- **brainstorming 禁令必须保留**：不然 claude 又发散（#4 复发）。
- **PTY vs headless**：headless stream-json 解 AskUserQuestion 更干净（推荐）；若必须 PTY，awaiting_detector 要加 claude AskUserQuestion pattern。
- **claude 网关**：若 open.bigmodel.cn 529，claude 不可用 → 全 kimi（kimi 提问机制不同，核实）。

---

## 12. 参考

- `docs/real-story-e2e-runbook.md` §7.3（#4 真因）/§7.4（design 飞轮注入 + a34cc843 + 三重验证）
- `docs/agent-decision-layers-rollout.md`（五层架构，层1 supervisor）
- memory（本机）：`design-flywheel-injection-blueprint`、`story-lifecycle-claude-design-heavy`
- 已 commit：`a34cc843`（design 维度+security+禁 brainstorming）、`aaa02b87`（supervisor→planner 接线）
