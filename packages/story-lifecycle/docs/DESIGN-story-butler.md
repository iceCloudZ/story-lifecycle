# 管家系统(story-butler) — 微信前门 + MCP 工具面 + 事件出口路由

> 状态:已定稿,待实现(v1 = §7 Phase 1)。创建:2026-09-01,由与用户的多轮探讨收敛而成。
> 范围:`packages/story-lifecycle`(通知通道 seam / 事件出口 / MCP server / 令牌代理)+ 101 服务器 clawbot-inbox 改造 + 桌面 Windows 运维件(隧道保活)+ `.claude/skills/`(三 skill)。
> **本文自包含**:拓扑、组件契约、降级矩阵、验收线、测试策略全部内联,执行者无需阅读其他文档即可开工。

---

## 0. TL;DR(执行者先读)

使用形态已迁移:全自动模式基本弃用,真实需求以 zcode 交互会话(每需求一个会话)为主,编排器退居后台状态机。缺的是一个**管家**:几个常驻对话窗口管进度 + 微信主动叫人 + 手机上能做轻确认。

方案四件套:

1. **事件出口 + 通知通道 seam + 路由表**(story-lifecycle 内,Python)——编排线程的决策点发事件,按路由决策表分档(打断/攒批/digest)投递;
2. **MCP server**(管家工具面,桌面 localhost,zcode 消费);
3. **微信通道**——clawbot(101,已有双向 bot)加 story 指令与短码确认;桌面↔101 用反向 SSH 隧道 + 桌面令牌代理收口;
4. **zcode 三 skill**(进行中/排查/评审)。

**关键设计决策(已拍板,不再讨论)**:

- **agent 只管家不写码**:编码永远是 zcode 会话的事,管家工具面没有任何代码执行能力。
- **DB 是唯一事实源/账本**:所有推进(桌面会话/微信短码/UI)经同一 API 落库,`confirmed_via` 字段标记来源;对话记录只是影子。
- **微信不做高危操作**:终态/发布类确认只在桌面评审 skill 里做;令牌代理白名单在服务端拒绝。
- **不做第二调度器**:事件出口只观察、只发通知,绝不碰 story 状态——守"编排线程是唯一调度入口"的硬规则。
- **不上消息总线**:outbox 表 + 重试够用;不引 MQ/webhook broker。
- **dsh(DeepSeek Harness)是可选升级不是依赖**:全部能力放在可迁移层(MCP/配置/DB),宿主(zcode)可换。能力层不 import 任何宿主概念。
- **大脑在桌面、101 只放哑巴件**:桌面常开(serve+编排线程+judge+MCP+路由器+worktrees);101 只放 clawbot/提醒推送/eval 农场/exchange——公司代码与 story DB 不上云。

---

## 1. 背景与动机(压缩版)

- 用户已弃用全自动 profile,主力是 zcode per-story 交互会话;编排线程/门体系/judge/patrol 仍是有效资产。
- 三个缺口:①微信上被动获知进度并轻确认;②zcode 会话里缺一个"管家嘴和手"(工具面);③决策账本只有机器侧(`orchestrator_decision`),人的确认没有来源标记。
- 行业验证(2026-09 检索):交互式 conductor 与编排器长期共存(Osmani);Telegram/微信 bot 作为 agent 通知+审批前门已是显学;MCP "build once, use everywhere" 是能力层规范做法。本方案是这些模式的同构实现。

## 2. 拓扑与分层

```
桌面(常开,大脑)──────────────────────────
  story serve :8180 + 编排线程 + judge        101(哑巴件)
  令牌代理 :18181 ←── 反向隧道(出站ssh) ──→  sshd localhost:18180
  MCP server(stdio,zcode 消费)              clawbot-inbox(微信 iLink 双向)
  事件出口+路由表+outbox                      remind.py(推送)
  zcode:交互会话 + 管家会话 + 三 skill         eval 农场 / lifeos-exchange
  worktrees(D:/worktrees)
```

- 出方向(桌面→微信):`ssh 101 remind.py "<文本>"`,已生产化(brief_push 同链路)。
- 回方向(微信→桌面):clawbot 收到 → `http://127.0.0.1:18180`(101 本机视角)→ 隧道 → 桌面令牌代理 :18181 → 校验后转发 serve :8180。零入站端口;101 侧只绑 loopback(sshd 默认 `GatewayPorts=no`)+ 云安全组挡公网,双保险。

**五层可插拔(churn 演练:换掉任何宿主,只重写薄桥)**:

| 层 | 内容 | 寿命 |
|---|---|---|
| 能力层 | MCP server 工具目录(§3.2) | 永生(Python) |
| 策略层 | 路由决策表、免打扰、skill 文案(§3.6) | 永生(配置/markdown) |
| 账本层 | DB + `confirmed_via`(§4) | 永生 |
| 桥层 | 令牌代理 + 隧道 + clawbot 指令(§3.3-3.5) | 可抛弃(重写小时级) |
| 宿主层 | zcode(今天)/ dsh(可选升级) | 商品 |

## 3. 组件设计

### 3.1 通知通道 seam + 事件出口 + 路由表(WP1)

**Seam(Definition/Provider/Consumer,遵守仓库 capability-seam 约定)**:

- `infra/notification/base.py`:`NotificationChannel`(Definition)——`name: str`、`send(title: str, message: str, tier: str) -> bool`、`available() -> bool`。
- Providers:`DesktopPlyerChannel`(迁移现有 `engine/notify.py` 的 plyer 行为,`engine/notify.py` 保留为兼容 re-export);`WeChatRemindChannel`(ssh 到配置的 host 执行 `remind.py`,文本一条,超时 30s,失败返 False)。
- Consumer:事件出口(WP1 主体)。

**事件类型(v1 五种,均为"已发生事实"的观察)**:

| event_type | 触发点 | 默认档位 |
|---|---|---|
| `awaiting_question` | supervisor 命中 agent 提问(现有 `_notify_awaiting`) | 打断 |
| `stuck_detected` | supervisor 卡住规则(现有 stuck notify_fn 路径) | 打断 |
| `gate_waiting` | lifecycle 推进停在 ui_button 确认门 | 打断 |
| `judge_rejected` / `judge_escalated` | stage judge 判 reject / escalate | 打断 |
| `stage_completed` | stage 成果物判定通过 | 攒批 |

**Outbox(raw SQL,遵守 No-ORM)**:表 `notification_outbox(id, event_type, story_key, project, tier, title, message, payload_json, status[pending|sent|skipped|failed], attempts, last_error, created_at, sent_at)`。至少一次语义:投递成功才标 sent;失败退避重试(1m/5m/30m,attempts≥5 标 failed 保留)。

**路由器(纯 Decider,表驱动)**:`route(event) -> list[ChannelAction]`。规则来源:代码内默认表 + `config.yaml` 可覆盖(`notification:` 段:routes / quiet_hours / channels.wechat 的 ssh host+命令)。**路由器只读事件与配置,无任何副作用**;投递(ssh/plyer/写状态)归 Delivery 线程(Handler)。

**分档语义**:`interrupt`(立即走微信+桌面弹窗)、`batch`(只落桌面弹窗/攒着,进晨报)、`digest`(只进晨报)。**免打扰时段**(默认 22:00-07:30,可配):interrupt 降级为 batch 并在消息里标 deferred,不丢。

**接线方式**:supervisor 两处现有 `notify` 调用改为 `emit_event(...)`;`gate_waiting` 在 lifecycle 停门处(`advance_lifecycle_to_target` 停在 `_story_state_gate` 的分支)发射;judge 两处同理。emit 只写 outbox(同步、快),投递线程异步 drain(编排线程绝不因通知阻塞)。

**`confirmed_via`**:`PUT /api/story/{key}/advance` 与 MCP/代理的 advance 调用增加可选 `confirmed_via` 字段(`desktop|wechat|ui|api`),写进该次 log_event 的 payload。不改表结构。

### 3.2 MCP server(WP2)

- 位置 `orchestrator/mcp/butler_server.py`,**复用 `clarify_server.py` 的 stdio JSON-RPC 实现模式与 `write_mcp_config` 注入方式**(先读它)。
- 工具面 v1(全部经 HTTP 调 `http://127.0.0.1:8180`,不 import 内部模块——serve 必须在跑,启动失败给友好报错):
  - `story_list(status?)` — 活跃 story 摘要(key/标题/状态/阶段/是否停门)
  - `story_detail(key)` — 全量:状态、stage 进度、judge 摘要、确认门详情、下一步建议、证据指针
  - `patrol_summary()` — patrol 发现 + stuck 状态 + 各 story reject 预算余量
  - `story_advance(key, confirm_token, confirmed_via?)` — **服务端执法**:`confirm_token` 必须精确等于 `f"{story_key}:{target_state}"`(target_state 来自 story_detail 的门信息),不等即拒绝。终态类 target 一律拒绝(高危只在桌面评审 skill 里以完整上下文做,见 §3.6)。
  - `plan_confirm(key, confirm_token)` — 同上执法规则。
  - `session_register(key, stage, adapter, session_id)` — sessions 簿记(补 adapter 缺口:zcode 会话登记,断点续跑有据可查)。
- 账本:每个 advance/plan_confirm 调用带 `confirmed_via`(默认 `desktop`),透传到 API。

### 3.3 令牌代理(WP3)

- 桌面 `127.0.0.1:18181`,单文件小服务(标准库或仓库既有 web 依赖,不引新依赖)。
- 职责:X-Internal-Token 校验(401)→ 白名单路由映射(仅 `GET /status`、`GET /story/{key}/brief`、`POST /story/{key}/advance`、`GET /patrol/summary`,映射到 serve 对应端点)→ 请求日志(append 到 outbox 旁的日志文件)。
- **为什么是代理而不是给 serve 加 auth**:serve 是本地信任模型,不加全局鉴权;代理只挂在隧道口,收口面最小。

### 3.4 clawbot story 指令 + 短码确认(WP4,101 上,敏感件)

- 复用 `baoxian_call` 的 internal-API+token 模式,base url 改 `http://127.0.0.1:18180`(隧道口)。
- 新指令(v1 三条,保持 clawbot"无 LLM 纯管道"人设):
  - 「进度」— 拉态势摘要回微信(打断消息里也可直接带);
  - 「推进 <关键词>」— 查停门 story → 唯一命中直接进待确认;多命中列编号(复用「完成」的 `_pending` 多选模式,TTL 5 分钟)→ 回编号或「T」→ POST 代理 advance(`confirmed_via=wechat`)→ 回执「已推进 ✅」;
  - 推送消息格式约定:标题+证据摘要两行+「回 T 推进 / 回 W 看详情」。
- 约束:不改 clawbot 既有指令行为;改动走 `.bak-` 备份惯例;改完 `systemctl restart clawbot-inbox` 并验证「状态」。

### 3.5 反向隧道常驻(WP5)

- 专用 key:`ssh-keygen -t ed25519 -f ~/.ssh/id_butler_tunnel`;101 `authorized_keys` 追加一行:`restrict,port-forwarding,permitlisten="18180" ssh-ed25519 ...`(该身份无 shell/pty/agent,只能开这一个监听)。
- 桌面保活:`scripts/butler/tunnel_keepalive.py`——循环 `ssh -N -R 18180:127.0.0.1:18181 -i id_butler_tunnel -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 101`,断开 sleep 5 重连;每 5 分钟经隧道 `GET /status` 自检,连续 3 次失败 → **走出方向 remind.py 告警「隧道断了」**(管家发现自己哑了会喊)。
- Windows 常驻:计划任务(登录时启动 + 每小时保活拉起)。

### 3.6 zcode 三 skill(WP6)

- `.claude/skills/story-progress/`(进行中)、`story-patrol/`(排查)、`story-review/`(评审),格式照 `run-story-serve` skill;`git add -f`(该目录被 ignore 但有 force-add 先例)。
- 内容 = 触发词 + 首选 MCP 工具序列 + 纪律(决定前重读 DB,不信会话记忆;评审 skill 是唯一允许终态确认的场所)。

## 4. 账本与审计

每次 lifecycle 推进的完整链:证据(judge 摘要)→ 确认(桌面 utterance / 微信短码 / UI 点击)→ API 调用(`confirmed_via`)→ log_event 落库。审计查询:某 story 的每次推进,谁在什么信息下、经什么通道确认的,一条 SQL 可回放。

## 5. 降级矩阵

| 故障 | 行为 | 丢失 |
|---|---|---|
| 微信 token 失效(要重扫码) | push 发送失败 → outbox 重试 → 攒批;`status/bot-status.json` 已暴露,管家会话可读 | 0(迟到) |
| 隧道断 | 微信只推不能令;心跳告警;桌面会话照常操作 | 0 |
| 桌面重启 | serve 自启(计划任务),outbox 补推 | 0 |
| 101 失联 | push 攒 batch,恢复后补发 | 0 |
| serve 未启动 | MCP/代理工具报友好错误;clawbot「推进」回复「管家后端未启动」 | 0 |

## 6. 验收线(质量标准,联调用)

1. **打断经济**:默认路由下,一天打断 ≤ 个位数,每条对应一个待决策事项;
2. **两秒决策**:每条打断消息自带证据+建议+动作(回 T/回 W);
3. **降级不丢**:outbox 至少一次语义,故障注入(杀隧道/断 ssh)后恢复补发;
4. **账本完整**:微信回 T 与桌面会话推的两次 advance,`confirmed_via` 分别为 wechat/desktop,log_event 可回放。

## 7. Phase 划分

- **P1(本设计,WP1-6+联调)**:如上全部。
- **P2(候选,不在本次)**:LLM 组稿(打断消息带上下文摘要)、晨报 digest 定时器、zcode transcript miner 适配(飞轮闭合的另一段)、dsh 桥(若 zcode 体验硌手)、夜班车间(委托 story 桌面夜跑)。

## 8. Anti-patterns(评审时照此把关)

- 给 outbox/路由器加"顺手推进 story"的逻辑(第二调度器)。
- 任何形式的 agent 写码工具(文件/shell)进 MCP 工具面。
- 微信通道出现终态/发布类确认。
- 引入 MQ/webhook broker/消息总线依赖。
- 给 serve 全局加鉴权(应只经代理收口)。
- clawbot 接 LLM(保持纯管道)。
- 能力层 import zcode/dsh 特有概念(宿主可换的前提)。

## 9. 测试策略

- WP1:路由器表驱动单测(事件×配置→动作,含免打扰降级);outbox 状态机单测(重试/失败保留/attempts);channel mock(不真 ssh);supervisor 接线点回归(现有 notify 测试迁移)。
- WP2:工具 schema 单测;confirm_token 执法单测(错 token/终态拒绝);mock serve(HTTP 层 stub)。
- WP3:白名单外 404/token 错 401/放行路径转发正确。
- 集成:`tests/integration` 一个冒烟——起 serve(或 stub)→ 触发 gate_waiting → outbox 落行 → fake channel 收到。

## 10. 决策记录

| 日期 | 决策 | 起因 |
|---|---|---|
| 2026-09-01 | dsh 从"方案组成部分"降级为"可选升级" | 探讨收敛:能力放永生层,宿主商品化;zcode 现成够用 |
| 2026-09-01 | MCP server 放桌面 localhost(原议 101 remote) | 大脑在桌面,zcode 本地消费,零网络面 |
| 2026-09-01 | "一切上 101"否决 | 安全:公司代码/DB 不上云;桌面常开替代 |
| 2026-09-01 | 账本从"dsh 会话日志"改为 DB+confirmed_via | 会话只是影子,DB 永生 |
| 2026-09-01 | 夜班车间(委托夜跑)推迟 P2 | 形态一(日落而息)先闭环;监督体验先行 |
