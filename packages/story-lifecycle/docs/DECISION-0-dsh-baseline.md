# DECISION-0: dsh 基线选择 — 已装 rc.6 vs master 源码

> **所属系列**:`PROPOSAL-dsh-plugin-integration.md`(方案)→ `REVIEW-…`(评审)→ 本文(**决策 0**)。评审 §9 指出"决策 0 排第一,它决定 worker 模型和桥接通道"。
> **问题**:story-lifecycle 的 dsh 集成(形态1/形态2)建立在**哪个 dsh 基线上**——本机已装的 npm `0.1.0-rc.6`,还是 deepseek-harness **master 源码**?
> **日期**:2026-08-14。所有事实已核实(见附录证据索引),非推断。

---

## 0. TL;DR

1. **本机装的就是最新发布线**:npm 官方 registry 上 `@deepseek-ai/dsh` 最新版 = `0.1.0-rc.6`(2026-08-13 发布),本机全局装的就是它。**不存在"更新稳定版可换"**;`latest` tag 陈旧停在 `0.0.1-rc.1`,`next` tag = `0.1.0-rc.6`。
2. **master 独有能力**(rc.6 没有):`sdk/`(JSON-RPC 出进程 SDK)、`acp/`、`hooks/`(Claude Code / Codex 线协议)、`subagent-codex` / `subagent-dsh-sdk`(外部 CLI 委派 provider)、`terminal/`(持久 PTY 能力族)、`lsp/`、`extensions/`(agent 运行期自改插件)、`guard/`、`context/`(workspace 指令)、`runtime-diagnostics`、`e2b`。
3. **关键转折**:master 时代的外部委派包**已发布到 npm `next`(0.1.0-rc.6),peerDependencies 与已装核心完全同线**(`^0.1.0-rc.6` + `cordis ^4.0.1`)→ **不必整体换 master**,可以"rc.6 核心 + npm next 插件"混用(路径 C)。
4. **推荐路径 C**:story 形态1 的核心层(judge/stage/知识注入)用的 seam(事件流、slot、session.jsonl、工具)rc.6 全有且与 master 一致;master 独有的外部委派按需以 `next` 包拉取。**整体换 master(路径 B)只在一种情况下需要**:PoC 证明 next 包与 rc.6 运行时不可兼容,或 story 需要 `terminal/`/`lsp/` 等 master 独有且等不到发布的族。
5. **决定性验证是一个 30 分钟 PoC**:`dsh plugin --profile web add @deepseek-ai/dsh-subagent-codex@next` + 一行 preset 接线,看能否在 rc.6 里跑通。peerDeps 匹配 ≠ 运行时兼容,必须实证。
6. **运营前置**:本机**没有 pnpm**(`dsh plugin` 转发 pnpm、master 构建都要用)→ 先 `corepack enable pnpm`。本机 npm registry 是 npmmirror 且**镜像陈旧**(曾返回过期的 0.0.1-rc.1)→ 拉 `next` 包需指定 `--registry https://registry.npmjs.org` 或等同步。

---

## 1. 事实基础(全部已核实)

| 事实 | 值 | 证据 |
|---|---|---|
| npm 最新发布 | `0.1.0-rc.6`,2026-08-13T12:40Z | 官方 registry `npm view @deepseek-ai/dsh` |
| npm dist-tags | `latest`=`0.0.1-rc.1`(陈旧),`next`=`0.1.0-rc.6` | 同上 |
| 本机全局安装 | `0.1.0-rc.6` | `C:\Users\zzh58\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\package.json` |
| master HEAD | `47f9438`,2026-08-13 19:38(PR #2519 feat/npm-public) | 浅克隆 `%TEMP%\dsh-master` |
| master 版本号 | root/apps-cli = `0.1.0-rc.5`,部分包 rc.5/rc.6 混标 | 克隆内 package.json |
| 外部委派包发布 | `@deepseek-ai/dsh-subagent-codex`、`dsh-hooks-codex`、`dsh-subagent-dsh-sdk`、`dsh-subagent-acp` 均有 `0.1.0-rc.6`(`next` tag) | 官方 registry |
| 本机工具链 | node v22.23.2 ✅,**pnpm 缺失** ❌ | `node --version` / `pnpm --version` |

**结论**:npm rc.6 与 master HEAD 同代(相差数小时/几个 rc 号),API 面基本同;npm 是发布卫生一般的 pre-release 线(`latest` tag 陈旧、版本号混标)。

---

## 2. master 独有能力清单(rc.6 没有)及其对 story 的价值

| 能力族 | master 位置 | 对 story 的价值 | 能否 npm `next` 拉取 |
|---|---|---|---|
| **hooks/**(Claude Code / Codex 线协议) | `packages/hooks/hooks-codex` | 直接驱动外部 CLI 当 worker(R3 的原始设想) | ✅ `@deepseek-ai/dsh-hooks-codex@next` |
| **subagent-codex**(one-shot Codex 委派) | `packages/subagent/subagent-codex` | 委派 codex 做 coding,不用自己写 spawn | ✅ `@deepseek-ai/dsh-subagent-codex@next` |
| **sdk/**(JSON-RPC 出进程 SDK) | `packages/subagent/subagent-dsh-sdk` + `dsh-sdk-protocol` | 提案形态2 的"Python 经 JSON-RPC 当 provider"通道 | ✅ `@deepseek-ai/dsh-subagent-dsh-sdk@next` |
| **acp/**(Agent Client Protocol 服务端) | `packages/subagent/subagent-acp` | 让任意 ACP 客户端接入 | ✅ `@deepseek-ai/dsh-subagent-acp@next` |
| **terminal/**(持久 PTY 能力族) | `packages/terminal` | story 的 PTY 查看器/会话管理有了原生落点 | ❓ 未见独立 next 包发布 |
| **lsp/** | `packages/lsp` | 代码语义能力 | ❓ |
| **extensions/**(运行期自改插件) | `packages/extensions` | story 工具动态注册(免重启) | ❓ |
| **preset/**(每 session 组合) | 已并入 rc.6 的 agent-presets | rc.6 已有同机制(见 §4) | — |
| **context/**(workspace 指令=AGENTS.md) | `packages/context` | 把 story 的运营指令挂进 prompt | ❓ |
| **guard/**、**e2b**、**runtime-diagnostics** | 各包 | 低相关 | ❓ |

> ❓ = 未见单独发布(在 master 源码里,是否随 `@deepseek-ai/dsh` 或其它 bundle 发布未逐一核实,PoC 时查)。

---

## 3. 三路径对比

| 维度 | A. 纯 rc.6(现状) | B. 整体换 master(源码构建) | C. **rc.6 + npm next 混用(推荐)** |
|---|---|---|---|
| 外部 CLI 委派(hooks/subagent-codex) | ❌ 无 | ✅ 全有 | ⚠️ 可拉,需 PoC 实证兼容 |
| sdk/ JSON-RPC(Python 桥) | ❌ 无 | ✅ 有 | ⚠️ 可拉,同左 |
| terminal/ PTY、lsp/ 等 | ❌ 无 | ✅ 有 | ❌ 无(等发布或转 B) |
| 当前 :3080 GUI / 会话 / 配置 | 不动 | 换环境(建议独立 `DSH_HOME`+端口) | 不动 |
| 环境稳定性 | 最高 | 低(master 每日移动) | 高 |
| 构建成本 | 0 | `pnpm install` + `pnpm run build` 全仓 | 0(只装插件) |
| churn 暴露面(R1) | 最小 | 最大 | 中(只追固定版本号) |
| 版本锁定 | `0.1.0-rc.6` | 无 tag,只能锁 commit | 核心 rc.6 + next 包锁 `0.1.0-rc.6` |
| 风险点 | 缺 master 能力 | storages/sessions 与 rc.6 混写风险;pnpm 全仓构建 | peerDeps 匹配≠运行时兼容(30 分钟 PoC 定) |

---

## 4. seam 稳定性证据(story 形态1 要碰的 5 个面,rc.6 vs master 一致)

| seam | rc.6(已装) | master | 结论 |
|---|---|---|---|
| 事件/waterfall(SessionEventMap) | `dsh-commands/lib/typert.host.js:398` 完整声明;`agent/pre-step` waterfall、`agent/turn-stopping` serial、`system-prompt/assemble` waterfall | `docs/agent-lifecycle.md` 时序图逐条一致(claim→pre-step→step→assemble→request→chunk→message→tool/call→tool/result→turn-stopping→turn/end) | ✅ 同 |
| slot(SlotCore) | `dsh-client-ui-slots/lib/index.js`:`single/keyed/list/chain` + `register(options, component)` + children 声明表 + priority shadowing | `packages/client/ui-slots/src/index.ts`:同 `register` + SlotMap 声明合并(类型更富,运行时同形) | ✅ 同 |
| client 清单(`dsh.client` + `exports["./client"]`) | `dsh-client-modules/lib/index.js` 扫描 loader 条目,组装 `__DSH_BOOT__`,服务 `/plugins/<id>/client.js` | 同机制(client/README) | ✅ 同 |
| session 持久化 | `$DSH_HOME/sessions/<projectKey>/<sessionId>/session.jsonl` 明文 | 同(`packages/session/persistence-jsonl`) | ✅ 同 |
| agent-presets 接线 | rc.6 npm 包自带 `config/agent-presets/{standard,code,cordis,minimal}/agent.cordis.yml` | 同机制;**subagent-codex 在 master 就是经 `agent.cordis.yml` 接线**(`apps/cli/config/agent-presets/standard/agent.cordis.yml`) | ✅ 同(意味着 C 路径的接线姿势可直接抄 master) |

**结论**:story 形态1 核心层依赖的全部 seam 在 rc.6 与 master 之间稳定。**PoC 不需要 master**。

---

## 5. 兼容性证据:为什么 C(混用)值得赌

1. `@deepseek-ai/dsh-subagent-codex@0.1.0-rc.6` peerDeps:`dsh-llm/dsh-session/dsh-subagent/dsh-subprocess/dsh-timeout/dsh-invariants/dsh-sdk-protocol` 全 `^0.1.0-rc.6`,`cordis ^4.0.1` —— **与已装核心版本线逐字一致**。
2. rc.6 缺的 peer(`dsh-sdk-protocol`、`dsh-hook-protocol` 等)由 pnpm 自动装进 profile 的 node_modules,不碰核心。
3. 接线姿势已明确:这些包**不声明 `dsh.bundle`**(npm view 无 `dsh` 字段)→ 不会自动进 bundle 层;正确姿势是**在 profile 的 `cordis.patch.yml`(或我们自己的 story bundle)里 insert 行** `{id: subagent-codex, name: '@deepseek-ai/dsh-subagent-codex'}`(与 master preset 的写法同构)。
4. **但 peerDeps 匹配 ≠ 运行时兼容**:rc.6 核心 bundle 里 subagent provider registry 的接口若与 master-era 包有隐式漂移,加载即炸。**这是唯一需要实证的点,30 分钟 PoC 定生死**(见 §7 步骤 2)。

---

## 6. 决策 0 对提案各问题的后果

| 提案项 | 决策 0 之后的最新状态 |
|---|---|
| R3(subagent 委派 claude/codex) | 不再是"未来可能":master 已有(subagent-codex/hooks),且 **npm `next` 可拉**。选 C → PoC 验证 rc.6 兼容;选 B → 直接可用。 |
| 形态2 / R5(飞轮 Python 桥) | master 有 `sdk/`(JSON-RPC)通道;rc.6 下维持评审结论——**miner 读 `session.jsonl` + TS 插件 fetch Python :8180**,不依赖 sdk。若选 C 且后续拉 `subagent-dsh-sdk`,可补 JSON-RPC 通道。 |
| R1(preview churn) | **加重**:`latest` tag 陈旧 + 版本号混标(rc.5/rc.6)证明发布卫生一般 → 无论哪条路,**锁具体版本号,不追 latest**。 |
| worker 模型 | C 路径下 story worker 先 = dsh 自家 agent;外部 CLI 委派作为"可选增强"逐步验证(先 codex,再 hooks)。 |
| 形态1 是否要等 master | **不用**:形态1 的核心层(judge/stage/知识注入/UI)所有 seam rc.6 齐备。 |

---

## 7. 推荐路径与决策步骤

**推荐 C**:核心留在已装 rc.6(:3080 GUI、会话、配置全部不动),外部委派按需拉 `next` 包;只有 PoC 证伪或 story 需要 terminal/lsp 时才转 B。

执行步骤(按序):

1. **装 pnpm**(本机缺):`corepack enable pnpm`(node 22 自带 corepack)。`dsh plugin` 与 master 构建都依赖它。
2. **PoC-0(30 分钟,定 C 生死)**:在 web profile 里拉 next 包 + 接线:
   - `dsh plugin --profile web add @deepseek-ai/dsh-subagent-codex@0.1.0-rc.6 --registry https://registry.npmjs.org`(npmmirror 陈旧,显式官方 registry)
   - profile `cordis.patch.yml` insert `{id: subagent-codex, name: '@deepseek-ai/dsh-subagent-codex'}`
   - 重启 :3080,确认加载无错;再验证一个 `subagent` 委派调用是否真能派给 codex。
3. **PoC-0b(若 2 失败)**:临时 `DSH_HOME=%TEMP%\dsh-master-home` + master 构建版(`%TEMP%\dsh-master`,`pnpm install && pnpm run build`,`pnpm dsh --profile web` 或 `dsh` 指向构建),验证 hooks 委派——证明 B 可行,作为 fallback 记录在案。
4. **按 2/3 结果定 A/B/C**,然后才进 story 重写 PoC(ui-story-* 包 + story_* 工具)。

---

## 附录:证据索引

**npm(官方 registry)**:`npm view --registry https://registry.npmjs.org @deepseek-ai/dsh-subagent-codex versions time dist-tags`(0.0.1-rc.1 → 0.1.0-rc.6,`next`=0.1.0-rc.6,`latest`=0.0.1-rc.1);同法查 `dsh-hooks-codex` / `dsh-subagent-dsh-sdk` / `dsh-subagent-acp`。

**master 克隆** `%TEMP%\dsh-master`(HEAD `47f9438`,2026-08-13):
- `packages/README.md`:49 个能力组,新增 sdk/acp/hooks/terminal/lsp/extensions/guard/context/preset 等
- `docs/agent-lifecycle.md`:waterfall 时序图(与 rc.6 SessionEventMap 一致)
- `packages/client/ui-slots/src/index.ts`:register + SlotMap(与 rc.6 SlotCore 同形)
- `packages/subagent/subagent-codex/package.json`:main=lib/index.js,**无 `dsh` 字段**(非 bundle)
- `apps/cli/config/agent-presets/standard/agent.cordis.yml` + `code/…`:subagent-codex 接线姿势
- `packages/session/persistence-jsonl`:`session.jsonl` 明文落盘

**rc.6 安装** `C:\Users\zzh58\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh`:
- `package.json`:`0.1.0-rc.6`;`config/agent-presets/{standard,code,cordis,minimal}/` 自带
- `node_modules/@deepseek-ai/`:194 包,**无** sdk/acp/hooks/subagent-codex/terminal/lsp 等

**本机**:node v22.23.2;pnpm 缺失;npm registry 指向 npmmirror(陈旧,曾返回 0.0.1-rc.1)。
