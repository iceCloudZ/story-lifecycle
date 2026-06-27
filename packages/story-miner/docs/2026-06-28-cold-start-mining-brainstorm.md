# 冷启动挖矿 — 发散思考

> 日期：2026-06-28（续作 2026-06-29）
> 状态：**点 3 注入设计 + 点 6 门禁设计已确认**；待续 = 结果轴挖矿机制（需 TAPD 数据访问）+ 数据核实项 + merge 策略
> 关联：`transcript-signals-ideas.md`、`2026-06-27-phase1-tier0-*.md`

## 背景 & 策略

半自动流程产出了 68 个 story（实际代码在 hc-all 工作区，不在本仓库）。全自动（story-lifecycle agent-mode 编排器）目前**在真实工作上一格都没跑过**。

**思路**：先对存量挖一轮冷启动飞轮，再让全自动跑新需求（暖启动）。
- 一石二鸟：冷启动既填了飞轮（现在几乎只有 SWE-bench 一份知识），又让全自动**暖启动**而不是空手上路。
- 比单纯"拿 68 个去试车全自动"更进一步：试车只是验证，冷启动是**先把弹药备好再上膛**。

---

## 11 个发散点

### A. 冷启动这个动作本身的特质（容易被当普通 refresh 跑掉）

**1. 冷启动是"一次性特殊模式"，不是日常增量。**
68 个是冻结的历史数据，没有新 session 涌入——可以批量、反复迭代挖矿逻辑。而且它**定调飞轮的未来**：种子里混进去的脏东西会一直污染下游。该当成 **"epoch 0" 认真做**，不是 `refresh.sh` 跑一遍就完。

**2. 最大的概念风险：种子会继承半自动的偏见。**
飞轮学的是"半自动是怎么干的"，包括它的盲区。比如半自动如果系统性不写测试、或总是过度设计，全自动就会把这些当"正常"继承。**知识要标成"观察到的模式"，不是"最佳实践"**，否则全自动把半自动的坏习惯也学走了。

**3. 冷启动产出必须落在"全自动读取的地方"，否则白挖。**
make-or-break 的集成点：全自动的 `{transcript_context}` 现在从 miner provider（读 transcripts.db）来，而 `packages/knowledge` 是另一套 `INDEX.json`。**冷启动挖出的 playbook/failure 要落到全自动 prompt renderer 真正会读的那个源**。这条线没接通，冷启动就是个自嗨的数据集。

### B. 挖什么、怎么挖

**4. 别只挖 68 个 story-bound。**
实测全量绑定率只有 ~20%（67/331），**unbound 占 ~80%（264/331）才是主力信号**——非 story 的日常开发全在那。（注：hc-all"~80% 绑定"是 story-sign 子集口径，非全量。）冷启动要**两半都挖**：story-bound 给"按 story 的 retrospect/偏差"，unbound 给"按 task-type 的 playbook/高频模式"。

**5. 冷启动会顺手画出一张"知识盲区地图"。**
60/68 如果都是后端 API 类，前端/数据/基建的 playbook 就是薄的——**正好告诉你全自动在哪些 task-type 上会弱**。盲区地图本身是高价值产出，不只 playbook。

**6. failure-mode 别只当注入的 prose，要变成全自动的"主动检查项"。**
冷启动挖出的失败模式（缺 CSRF、没测试、空指针…）可做成 verify 阶段跑的 checklist。**这是冷启动数据最能直接提升全自动产出质量的地方**——从"给提示词加点上下文"升级到"给一道硬检查"。

**7. 冷启动让"飞轮到底有没有用"第一次可测。**
全自动跑新需求时，做一次 **with vs without 注入上下文的 A/B**（同需求、关掉/打开 provider）。这是项目核心命题的第一个实测，而**只有冷启动把知识备好了，这个对照才成立**。`llm_trace` 已在记，不用额外埋点。

**8. 和 Tier-0 基本解耦，不用等。**
分析器对 DB 只读（`query_only=1`）——但注意冷启动的**先 ingest+link 那步是写的**（`link.py:219` ALTER、`store.py` RW ingester）。**【已修正】Tier-0 的 adapter 3-tuple + `token_usage` + `story_token.py` 在当前仓库已落地**（不再是 pending）；定性知识（playbook/failure/retrospect）现在就能挖，定量（token/cost）也基本可跑（建议和 Tier-0 窗口确认 `full_ts` / per-adapter token 采集是否 100% 完成再下定量结论）。

**9. 冷启动必须做 QA：头几份 playbook 人审。**
种子的质量门槛 = 飞轮一辈子的门槛。先人审 3–5 份 playbook，确认"真的有用、没把合成数据当真"——定了标准再放批量。

### C. 两个更野的发散想法

**10. 反向冷启动：把 68 个的 design/spec 冻结成"全自动必须能复现"的 eval 集。**
冷启动顺带把**回归基线**建了——以后每轮挖矿/每个全自动改进，都能拿这 68 个当尺子量"有没有退步"。

**11. 半自动→全自动的 diff 挖矿。**
对能两边都跑的 story，专门挖"全自动做得和半自动不一样的地方"，把这批差异当**元知识**（"全自动倾向于怎么偏离半自动"）。飞轮从"记录过去"走向"理解自己"的一步。

---

## 11 点验证结果（子代理初步核实，2026-06-28）

派 4 个只读子代理核实 11 点。VERDICT：✅ 站得住 / 🟡 部分·需修正 / ❌ 过度或 greenfield。

| 点 | V | 核实结论（证据 file:line） |
|---|---|---|
| 1 epoch-0 冻结 | 🟡 | ingest 是**增量**非冻结（`store.py:89-100` mtime、`refresh.sh --since-days`），无 snapshot/版本钉。"冻结"取决于 hc-all 是否停更，本 repo 不可证。 |
| 2 种子继承偏见 / 标"观察"非"最佳" | 🟡 | 前提对，但 schema 有 `source`/`status`/`source_refs` 钩子（`knowledge/models.py:15-22`）却**没 miner 代码用它**区分（`generate_playbooks.py:315` 写死 `source:dynamic`）；linkage 分档**纯文档、无 enum/字段**。偏见证据需 hc-all transcript。 |
| 3 产出要落到全自动读取处 | ✅ | **确认 disjoint**：全自动只读 `transcripts.db`（miner provider，`context_providers/__init__.py:52-68`）+ 松散 `.story-knowledge/{key}/*.md`；`INDEX.json` 在 story-lifecycle/src **零引用**。→ 冷启动产出若只进 INDEX.json = 白挖。**#1 集成闸。** |
| 4 也要挖 unbound | ✅+修 | 实测 `transcripts.db`：**unbound 264/331 = 79.8%**；hc-all 内 129/193=67% unbound。**【修正】原文"hc-all ~80% 绑定"是 story-sign 子集（41/51），不是全量；全量绑定只 ~20%。** unbound 确是主力，比原文更极端。 |
| 5 盲区地图 | 🟡 | 机制在（`generate_playbooks.py:19-27` 7-theme 标签）；但 68 真实分布**在 hc-all，本 repo 不可证**。 |
| 6 failure→verify checklist | 🟡 | checklist builder **已存在**（`quality.py:187 build_quality_checklist`），但默认 profile 关了 quality/adversarial（`minimal.yaml:53-57`）、`verify.md` 无 `{quality_checklist}` 占位、对抗循环死代码。→ 有种子，需"接进 verify + 开 quality"。 |
| 7 A/B 可测 | 🟡 | 无直接开关（只能间接：miner 不可导入 / stub provider 返 None）；`llm_trace` 太薄——**不记 prompt 内容、不记注入状态**（`llm_client.py:466-467` 写死 story_key=""）。A/B 可做但需额外埋点。 |
| 8 只读 / 不依赖 Tier-0 | 🟡 修 | (a) 分析器只读（`query_only=1`），但 **`link.py:219` 写 schema**（`ALTER sessions ADD story_id`）、`store.py` 是 RW ingester——冷启动的"先 ingest+link"那步是写的。(b) **【重大修正】Tier-0 的 adapter 3-tuple + `token_usage` 表 + `story_token.py` 在当前仓库已落地**（`store.py:105`、三 adapter 返 3-tuple），**不再是"0%/pending"**。定性挖矿与 Tier-0 解耦仍成立。 |
| 9 人审头几份 | 🟡 | 手动可行，但**无 playbook 审核基建**：现有 review 只管 `learned_pattern`（DB）/ `delivery_artifact`（merge）/ `review_feedback`（code），**不管 playbook**。playbook 是覆盖式写文件（`generate_playbooks.py:309`），无 review_state/draft 字段。 |
| 10 68 当 eval 基线 | ❌ | 68 目录在，但**冻结不均**（36/68 design.json、33/68 plan_design.md），代码在 hc-all。E2E harness **存在**（`packages/testing/harness.py run_real_story` + calculator）但只做**结构断言**（文件存在/非空/pytest-0），无 golden/内容对比，也没拿 68 做场景。→ 脚手架，非现成基线。 |
| 11 半→全 diff 挖矿 | ❌ | **零现成能力**。`retrospect.py` 是单会话复盘，无任何两版/diff 对比。纯 greenfield。 |

### 验证带出的 3 个必须修正

1. **Tier-0 adapter 层已落地**（点 8b）——原文"等 Tier-0"措辞 stale。quantitative 挖矿现在可能就能跑（建议和 Tier-0 窗口确认 `full_ts` / per-adapter token 采集是否 100% 完成再下定量结论）。
2. **绑定率口径**（点 4）——"~80%"是 sign 子集；全量 ~20%、unbound ~80%。冷启动别低估 unbound 体量。
3. **INDEX.json 与全自动 disjoint**（点 3）——冷启动产出**必须**进 transcripts.db 支撑的 provider 或松散 `.story-knowledge/*.md`，进 INDEX.json 全自动看不到。这是"冷启动能否喂到全自动"的 #1 闸，目前仍未解。

> 附：`transcripts.db` 已有 331 session（67 bound）——非空；但是否**覆盖全 68 个 story 的 transcript** 待确认。

---

## 点 3 最终设计：统一引用注入模型（已收敛 2026-06-28）

> 状态：**已确认设计**（替代早前"压缩包注入"思路）。这是"冷启动能否喂到全自动"的 #1 闸，本节是它的解。

### 总览流程

```
新需求 story
   │
   ▼
[design] ──LLM 填 task_type(受控词表)──► story.task_type 入库
   │
   ▼
get_knowledge_context(task_type)        ← 廉价路径映射，不重新生成
   │
   ▼   指向【已积累】的文件（见下"积累/缓存"节）：
      ├ playbook   .story/knowledge/playbooks/{type}.md   advisory
      ├ failures   .story/knowledge/failures/{type}.md    mandatory
      └ 先例 story  tapd-…655336 (带 self_check)          advisory
   │
   ▼   缓存 .story/context/{key}/knowledge_refs.md
   │
[design/build prompt] ◄── 注入 {knowledge_context} = 指针清单
   │
   ▼
AI CLI 按需读文件（advisory：相关才读）
   │
   ▼
[verify] ◄── {quality_checklist} = failure 清单 (mandatory)
   │
   ▼
gate 检查 mandatory 条目落地   ✓ / ✗
```

### 核心模型：渲染文件 → 注入引用 → AI 按需读

三个上下文槽统一成**一种**机制：把上下文渲染成文件 → prompt 注入指针/引用 → 全自动的 AI CLI（Claude Code/Codex，能读文件）按需读。

指针清单示意：

```
## 相关知识（按需阅读）
- 本任务归类：sms-marketing
- playbook：.story/knowledge/playbooks/sms-marketing.md
- 常见失败：.story/knowledge/failures/sms-marketing.md
- 相似先例：tapd-…655336（带 self_check）
```

好处：
- **预算问题全消失**——不再任何地方压 <500 字。
- AI 拿**全量**而非摘要。
- `prompt_renderer` 只剩一种注入机制（`{xxx_ref}` → 渲染好的文件），大幅简化。
- 各槽可独立开关 → A/B 天然成立。

### advisory vs mandatory：引用负责送达，gate 负责执行

引用是 advisory（AI 可跳过），但有些东西要 mandatory。分两类：

| 类 | 内容 | 处理 |
|---|---|---|
| **advisory** | playbook / transcript / 先例 story | 纯引用，AI 觉得相关才读 |
| **mandatory** | failure-checklist / 硬约束 | 引用 + **执行层**（verify/gate 检查落地） |

**解耦：引用把知识送到 AI 眼前，gate 强制它落地。** mandatory 那批配执行层 = 顺带复活了之前那批死 gate（点 6 的 failure→checklist 就在这里和门禁接通）。

### 三槽具体怎么变

- **`{knowledge_context}`（新）→ advisory**：按 `story.task_type` 渲染 playbook + 先例指针清单。
- **`{transcript_context}`（已有）→ advisory**：provider 不再返回 <500 字串，改为渲染历史到文件 + 引用（可后做）。
- **`{quality_checklist}`（已有槽，`prompt_renderer.py:352`）→ mandatory**：渲染 failure checklist 到文件 + 引用；另配 verify/gate 检查条目落地。

### 积累/缓存：引用模型天生摊销

"渲染文件"不是每 story 重做。分两层：

- **静态/共享层（挖一次、增量刷新）= 积累物**：playbook / failure / 先例 self_check 由 mining refresh 产出，**所有 story 共用、只增不减、越滚越富**。引用只是指向它们，不重新生成。
- **每 story 薄层**：只有一个 `task_type → 哪几个文件` 的指针清单。design 定 task_type 后缓存到 `.story/context/{key}/knowledge_refs.md`，仅在 task_type 变 / 该类型来了新 playbook·新先例时失效重算。

这正是指引模型比"每次压 <500 字包"强的地方：后者每次重算，前者摊销。

```
hc-all transcripts (331 session)
        │
        ▼  refresh.sh（增量 mtime）
   [mining] ── 按 task_type 聚合 ──►  .story/knowledge/
                                        ├ playbooks/{type}.md   ◄┐
                                        ├ failures/{type}.md     │ 所有 story 共用
                                        └ 先例 self_check/交付    │ 只增不减、越滚越富
                                                                  ┘
        ▲
        │ 每 story 只读指针，零重新生成
   [各 story 的 design/build/verify]
```

### task-type：受控词表 + LLM 填

- **词表**：**受控**（金融场景有限）。冷启动批量归类与 design 时填写用**同一套**。
- **冷启动**：对 68（+unbound）一次性 LLM 批量归类，产出带 `task_type` 语料；playbook 按语义类型建（不再只靠 `first_ucmd` 7-theme）。
- **后续**：design `expected_outputs` 加 `task_type`（挨着 `complexity`，同机制），design LLM 顺手填。存 story 字段。
- **检索 key** = `story.task_type`。

**词表（已确认 2026-06-28 · 基于 generate_playbooks 7-theme + 68 story 标题 + hc 服务名）：**

| task_type | 含义 | 种子关键词 | 旧 theme 来源 |
|---|---|---|---|
| `credit-limit` | 授信/额度/风控规则 | 授信、额度、风控 | credit-risk |
| `fund-flow` | 放款/还款/提现/清分/对账 | 放款、还款、提现、清分、溢缴款、对账 | credit-risk |
| `message-notify` | 消息/OTP/通知/模板 | 短信、sms、OTP、推送、通知、模板 | sms-marketing |
| `marketing` | 营销/活动/券/奖励 | 营销、活动、MGM、优惠券、免息 | sms-marketing |
| `user-profile` | 用户/资料/认证/隐私 | 用户、认证、职业、邮箱、联系人、隐私 | requirement-dev |
| `order` | 订单/交易 | 订单、交易 | 新增(hc-order) |
| `integration` | 第三方对接/回调 | 三方、回调、下游适配、对接 | 新增(hc-third-party/callback) |
| `gateway-infra` | 网关/限流/配置/调度/状态机 | 网关、限流、配置、定时任务、状态机 | 新增(hc-gateway/config/job) |
| `data-sql` | 数据/SQL/迁移 | sql、查询、schema、ddl、迁移 | data-sql |
| `frontend` | 前端/admin/页面 | 前端、admin、页面、组件、protable | frontend |
| `deploy` | 部署/上线/发版 | 部署、上线、发版、nexus | deploy |
| `debug` | 排查/定位 | 排查、debug、报错、日志、异常 | debug |

> 12 类 = 业务域（前 7）+ 横切类型（后 5）。**横切 5 类是合法场景但低频，playbook 会偏稀疏（呼应点 5 盲区地图）；词表范围 = 本用户 dev 范围，催收/合规/账务等属其他团队、不在内。** 旧 7-theme 都能映射进来，冷启动向后兼容。若要更细可加二级 nature 标签（feature-dev / bugfix / refactor）。

**task-type 来源流程：**

```
冷启动（一次性）                  后续（每新需求）
───────────────                  ──────────────
68 + unbound session              新需求 story
        │                              │
        ▼                              ▼
  LLM 批量归类                   [design] LLM 顺手填
  (受控词表 · 12 类)             (expected_outputs += task_type)
        │                              │
        └───────► story.task_type ◄────┘
                 (同一套受控词表)
                      │
                      ▼
            playbook / 先例 检索 key
```

### 先例 story：带上

指针清单带"相似先例"。选取：**task-type 匹配 + 优先带 self_check / 已交付证据的**。

### A/B：引用模型下能量三层

引用模型把"飞轮有没有用"拆成可诊断的三层：
- 指针**没给** → baseline。
- 给了、AI **没读**（transcript 的 file-read 事件可查）→ 是 prompt/nudge 问题。
- 给了、**读了**、还没用 → 才是**知识本身**没用。

### 演进路径

1. **v1**：task-type → 路径映射（确定性、可复现）。getter = `story.task_type → 文件路径清单`，不用 LLM、不用压缩。
2. **演进 1**：`knowledge.KnowledgeIndex.retrieve()` scored retrieval（跨类型智能排序先例）。
3. **演进 2**：design-LLM-assisted 检索（LLM 挑最相关先例）——语料大了再上，且注意保持 eval 可复现。

### v1 落地清单（最小可验证，待执行）

- [ ] story 加 `task_type` 字段 + 冷启动批量归类脚本（LLM，受控词表）。
- [ ] design `expected_outputs` += `task_type`。
- [ ] 新 getter `get_knowledge_context()`：`task_type → playbook/先例路径清单`（advisory，渲染文件 + 引用）。
- [ ] design/build 模板加 `{knowledge_context}` 引用槽。
- [ ] verify：failure 喂进 `{quality_checklist}`（已有槽）+ 配一个执行检查（mandatory）。
- [ ] transcript_context provider 改为渲染文件 + 引用（可后做）。
- [ ] A/B：开关 `{knowledge_context}`，transcript 记 file-read，量三层。

### 仍待你定

- ~~受控词表~~ → **已确认（12 类）**。

---

## 点 6 / 门禁最终设计：轻量混合 gate（研究背书，已收敛 2026-06-28）

> 状态：**已确认设计**。补完点 3 的 mandatory 半边（之前"needs 执行层 TBD"），同时复活 turn 1 那批死 gate，并接通点 6（failure→强制 checklist）。

### 问题

点 3 里 advisory（playbook/先例）靠引用、AI 可读可不读；但 mandatory（failure-checklist/约束）必须落地。现状：`{quality_checklist}` 槽在（`prompt_renderer.py:352`）、`build_quality_checklist`（`quality.py:187`）能建清单，但 quality 默认关、清单只是注入文字没人查；`gate.py` / `evaluator_loop.py` **零调用**，14 个 story 卡 `wait_confirm` 但 review rounds=0、零 findings。即"送到了但不强制"。

### 好消息 & 张力

- **机械基本都在**：`gate.py`、`evaluator_loop.py`、`repair_packet`、`build_quality_checklist` 都在，只差接线 + 喂料。
- **张力**：团队从重 LLM 对抗 gate（LangGraph）迁到 agent-mode，gate 被甩下。**复活不能把重对抗回路原样搬回**——得轻量。

### 研究背书的两条关键约束（会咬人的）

1. **LLM-judge 自增强偏见**：LLM 倾向认可自己/同族模型的产出。**若用 LLM-judge，必须换一个不同族的模型**，且只查机械覆盖不了的高危项。LLM-as-judge 生产环境错误率超 50%、93% 团队遇可靠性问题——别依赖它。
2. **反思式 repair，不是傻重试**：大多数"自纠正 agent"只是昂贵重试循环。真修复要把**失败反思注入**重试（Reflexion 式）。我们现成的 `repair_packet` 正是这个机制。

### 设计（逐选择钉死）

| 环节 | 机制 | 依据 |
|---|---|---|
| 主力检查 | pytest 退出码 / lint / 反模式 grep，**只查 `files_changed`** | "always run it"、Clean as You Code |
| 补充检查 | LLM spot-check，**换模型**，只查机械覆盖不了的高危项 | 自增强偏见 |
| forcing | done 握手对每条 failure-checklist 自证 `addressed: true/false/na` | 强制面对 |
| severity | HIGH block / MED warn / LOW 记录 | SonarQube / GitHub |
| fail | repair round 注入失败反思（`repair_packet`），封 `max_retries`→升级人工 | Reflexion / SWE-agent |
| 时机 | design = 轻 warn；verify = 真 gate | shift-left |
| 喂料 | `build_quality_checklist` += 按 `task_type` 冷启动 failure-modes | 点 6 |

### gate 流程图

```
[verify] AI 干活 + 写 done.json（含每条 failure 的 addressed 自证）
   │
   ▼
gate（编排器侧执行，不是执行器自审）:
   ├ 1. 解析 done.json 自证 —— 每条面对了吗？
   ├ 2. 机械检查：pytest / lint / 反模式 grep（只查 files_changed）
   └ 3. LLM spot-check（换模型，只查"自证 true 但机械查不了"的高危项）
   │
   ▼
severity 汇总：
   HIGH 失败 ──► repair round（失败项作 repair_packet 重注入，Reflexion 式反思）
   MED  失败 ──► warn，放行
   LOW       ──► 记录
   │
   ▼
repair 到 max_retries 仍 fail ──► 升级人工（不无限重试、不死等）
```

### 分离原则（架构）

执行器 AI 写代码；gate 由**编排器（story-lifecycle）侧**跑机械检查 + **另一个模型**做 spot-check。**不让同一个 agent 既写又判**（"学生给自己批卷"）。这正好用上编排器作为独立执行层的定位——机械检查是真相源，LLM 只补盲区。

### Sources

- LLM-judge 偏差/可靠性：[Adaline](https://www.adaline.ai/blog/llm-as-a-judge-reliability-bias) · [Galileo](https://galileo.ai/blog/why-llm-as-a-judge-fails) · [arXiv: Bias in the Loop](https://arxiv.org/html/2604.16790v1) · [W&B](https://wandb.ai/site/articles/exploring-llm-as-a-judge/)
- 自修复循环：[Reflexion (arXiv)](https://arxiv.org/abs/2303.11366) · [SWE-agent (Princeton)](https://github.com/swe-agent/swe-agent) · [RepairAgent (ICSE 2025)](https://arxiv.org/html/2403.17134v1) · [Self-correcting ≠ retry](https://medium.com/@Micheal-Lanham/self-correcting-agents-are-not-what-you-they-are-d19398186373)
- severity 分级：[SonarQube Severities](https://community.sonarsource.com/t/define-quality-gate-using-the-new-severities/109970) · [GitHub PR thresholds](https://docs.github.com/en/code-security/how-tos/maintain-quality-code/set-pr-thresholds) · [NDepend Quality Gates](https://www.ndepend.com/docs/quality-gates)
- 执行优先 / 分离审：[Run, don't read (Medium)](https://Medium.com/@haseeb_sohail/how-i-evaluate-llm-code-quality-reviewing-ai-generated-code-at-scale-db8c4f150107) · [Don't let the same agent write & verify (Dev.to)](https://dev.to/teppana88/how-i-validate-quality-when-ai-agents-write-my-code-481c)

---

## 结果轴 & linkage 分档（transcript-less 数据怎么用）

### transcript-less 数据有价值——它是 transcript 给不了的"结果层"

- transcript = **过程层**（怎么建的）；TAPD + master git = **结果层**（建得怎么样、上线后表现）。
- 飞轮目前跛：只有过程，没有结果。结果层补上半边。
- 结果层独有产物：feature→bug 图、真实 cycle-time、代码存活/churn、review 评论、revert/hotfix 模式、意图 vs 交付 gap。
- 最值钱的**交集**：既有 transcript 又有交付的 story → 过程↔结果相关 → 把"观察到的模式"升级成"被验证的最佳实践"，同时治好发散点 2 的种子偏见。

### 但结果层的可信度卡在 linkage 质量

- 68 个 story 有**分支名 → 可反查提交**（硬 linkage）。
- 更久远的 story 只能**凭记忆关联**（软 linkage），又漏又错。

**核心原则：linkage 精确 > 召回，错绑比漏绑更糟。** 交集分析是价值最高、也最敏感的产物，假 story↔code 绑定会制造假相关，污染唯一不能错的东西。宁可让老 story 暂不挂，也别猜着挂。

### linkage 分档（作为数据自带 provenance 字段）

| 档 | 来源 | 用途 |
|---|---|---|
| `hard:branch` | 68 个分支名反查 | 全交集分析，最高权重 |
| `hard:tapd-id` | commit 引了 TAPD id | 全分析 |
| `weak:triangulated` | 时间窗 + 文件重叠 + 作者 多信号收敛 | 粗粒度，中权重 |
| `memory:human` | 凭记忆 | 只趋势/聚合，低权重，永不进细粒度交集 |
| `none` | 挂不上 | 不用 |

交集分析**只吃前两档**。

### 冷启动因此分三层

- **Core（干净交集）= 68 个**：transcript + 分支反查 + 交付，全维度挖，含过程↔结果相关。无尘室训练/eval 集，**守住别稀释**。
- **Extended（结果轴粗粒度）= 久远但硬绑/三角绑的 story**：只挖结果轴粗信号，低置信，单独存放。
- **Skip = 记忆都挂不上 / 半自动前时代**：背景上下文，不当训练数据。

### 两个具体动作（已查代码，字段已定位）

1. **68 个的反查不用猜，字段已经在 DB 和 done 握手里**：
   - **分支名**：在 story DB 的 `story_project.branch`（+ `story.branches_json`），由 `orchestrator/branch_naming.py::generate_branch_for_story` 在建 story 时按 profile 的 `branch_rule` 铸出。**不在 `.story/context/` 的 markdown 里。**
   - **story→文件 直映**：`.story/context/{key}/done/*.json` 的 `files_changed` 直接给了"这个 story 改了哪些文件"——比分支更硬的 linkage，feature→bug 甚至不用翻 git。
   - **repo**：同文件的 `repos_modified`（repo 路径列表）。
   - 反查链：DB 取 `story_key → branch` + done 取 `files_changed` → 在对应 repo `git log` 该分支 / 追同文件后续 bug-fix = feature→bug。自动、硬绑、不依赖 Tier-0，可和过程轴并行。
2. **记忆关联趁现在捞，只留高置信的**：做一次轻量人工 pass，只标能确定的，挂不上归 `none`。捞到的标 `memory:human` 进粗粒度池，不进干净交集。

---

## 后续需求的硬关联保障（new stories 必须 hard-linkable）

> ⚠️ 探索代码后修正：**这套骨架大部分已经建好了**，不是从零搭。下面分"已存在"和"真正的缺口"。

目标：从今往后每个新需求都落到 `hard:branch` / `hard:tapd-id`，让 `memory:human` 档对新工作**灭绝**——"凭记忆关联"只是一次性历史包袱。

### 已存在（不用再造）

- **建 story 即铸分支**：`branch_naming.generate_branch_for_story` 按 profile 的 `branch_rule` 渲染分支名，占位符支持 `{author}/{date}/{summary}/{story_key}/{project}`。
- **DB 权威绑定**：`story_project`（`branch` / `base_branch` / `base_commit` / `worktree_path` / `worktree_state`）+ `story.branches_json`。story↔branch↔worktree 已在 DB 硬绑。
- **交付/合并证据表**：`story_delivery_artifact`——已有 `source_branch` / `target_branch` / `delivery_state` / `review_state` / **`merge_commit`** / `external_id`(PR/MR id) / `url`。**merge_sha 的归宿已经存在。**
- **agent 回填 API**：`PUT /api/story/{key}/context/branch`（docstring 写明 "agent backfill"），全自动跑的过程中可登记自己用的分支。
- **`story doctor` 命令已存在**（目前只查环境/路径，`doctor_paths` 扫 legacy `.story-*`）。

编排器当铸造方这点已经成立：full-auto 在铸好的分支上干活，id 是 by construction 的。所以"新需求硬关联"是**收口问题，不是从零建设**。

### 真正的缺口（按性价比排）

1. **【最高性价比】默认 `branch_rule` 没带 `{story_key}`**：`minimal`/`strict` 都是 `feature/{author}/{summary}_{date}`——**分支名里没有 id**，master 历史里没法 `git log --grep <id>`。修法：profile 一行改成 `feature/{story_key}/{summary}_{date}`（占位符已支持），分支名立刻自描述。**一行改动，全链受益。**
2. **没有 commit-msg hook**：提交不带 id。需要仓库级 `commit-msg` hook 从分支名注入 id（或拒绝无 id 提交）。（注意 story-miner 的 `hooks/` 是另一回事——Phase-2 未启用的骨架，别混。）
3. **没有 CI / PR 闸**：全仓无 `.github/workflows`，没有任何东西拦住"没带 id 的 merge"。需要 status check 校验"分支名↔id↔有效 open story"，配 branch protection。
4. **【已确认：无被动自动写回】`merge_commit` 只在显式调用时写入**：途径是 `CreateDeliveryRequest`（`api.py:2038`）和 `delivery.py` 的 `local_merge`（强制 `merge_commit` 非空，由调用方提供）。**全仓 grep `webhook|on_merge|merge_event` 零命中**——没有任何 merge 事件监听器被动抓 SHA。所以即便全自动，也得 agent 在 merge 后**主动**调 `create_local_merge_artifact` 记录，或补一个 GitLab/GitHub webhook → `update_delivery_artifact(merge_commit=…)`。
5. **`story doctor` 没查 linkage health**：加一个检查项——story 有无 branch？有无 delivery_artifact？有无 merge_commit？输出"% hard / 孤儿分支 / 无 id commit"报告。
6. **没有 hotfix 回挂约定**：后续 bug-fix 用 `Fixes <id>` trailer 绑回原 story，feature→bug 才能自动闭环。

### 边界情况

- **一 story 多 PR**：`story_delivery_artifact` 可存多条（每个 PR/MR 一行），不是 1:1。
- **abandoned（没 merge）**：`delivery_state` 不推进，`merge_commit` 留空，仍可挂分支。
- **一 PR 多 story**：不鼓励；真出现则 PR 带多个 id。
- **紧急逃生**：admin 可绕过，但记日志（别让 bypass 变常态）。

### 待确认

- ~~分支名存哪个字段~~ → **已确认：DB `story_project.branch` + `story.branches_json`**（不在 `.story/context/`）。
- **merge 策略**（squash / merge commit）：决定 id 是否进 master 历史（squash 要保证 message 带 id）。
- ~~`merge_commit` 自动写回~~ → **已确认无自动写回**，只有显式 API 写入，无 webhook。需补 merge 钩子（缺口 4）。

---

## 落地前要先确认的前提

- **hc-all 的 transcript 进 `transcripts.db` 了吗？** 没进就得先采集一轮——冷启动的真正前置。
- **全自动的 prompt renderer 读的是 miner provider 还是 `INDEX.json`？** 决定冷启动产出要落到哪个源（对应第 3 点）。
- **68 个里 synthetic 占比**（CR-* / `_synthetic_design` 那批）——冷启动前必须先过滤，否则 playbook 被合成规格污染。
- **task-type 分布会不会太偏**——偏的话盲区地图（第 5 点）会很尖锐，全自动适用范围一开始就被限死。
