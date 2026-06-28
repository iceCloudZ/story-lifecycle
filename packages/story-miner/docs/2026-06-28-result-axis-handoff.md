# 结果轴一期 — 并行 handoff brief（A / B / C）

> 日期：2026-06-28 · 分支：`feat/bug-story-graph`
> 设计依据：`packages/story-miner/docs/2026-06-28-cold-start-mining-brainstorm.md`（「结果轴一期最终设计」节）
> 汇总验收：由主窗口（Claude）负责——三份产物回来后合并 + 验收 + 出最终 outcome 知识。

每个 brief 独立可派给一个窗口。**各写各的脚本、各出各的 JSON，不编辑共享文件**（设计文档只读）。全部在 `feat/bug-story-graph` 分支上做。

---

## 共享上下文（三个 brief 都要用）

- **TAPD**：workspace `44381896`，owner `赵子豪`。访问经 `~/.claude/scripts/cli_tapd.py`（`TapdApi` 动态加载它的 `TAPDClient`）。config 在 `~/.story-lifecycle/config.yaml`（DeepSeek key 也在）。
- **参考脚本（已跑通）**：`packages/story-miner/scripts/bug_story_graph.py`——照它的脚手架：`sys.path` 加 miner + `packages/story-lifecycle/src`、`from story_lifecycle.sources.tapd_source import TapdSource`、argparse、输出到 `scripts/out/*.json`、`PYTHONIOENCODING=utf-8` 跑。
- **corpus 口径（关键）**：story = `get_stories({'entity_type':'stories','limit':500,'custom_field_25':'赵子豪'})` ≈ **150**（≈ story.db 的 147）。**不要用 `fetch_pending`（只给 20，漏大部分）。**
- **最大坑**：TAPD bug 的正向 `story_id` **全空（实测 0/142）**。bug↔story 必须**反向**：`get_related_bugs(story_id)` → `[{workspace_id, story_id, bug_id}]`，约 20% story 挂了 bug。
- **hc-all 是 multi-repo**：`D:/hc-all` 本身不是 git，17 个子仓在下面（hc-order/hc-user/hc-risk-management/hc-message/hc-config/hc-limit/hc-third-party/hc-coupon/hc-marketing/hc-callback/hc-gateway/hc-job/hc-audit/hc-aiops/hc-pytest/story-board/ys-frame-parent）。commit 在子项目里。
- **story_project.branch**：在 `~/.story-lifecycle/story.db` 的 `story_project` 表，**36 个 story 有 branch**（如 `feature/zzh/reject_message_config_0616`，base=master）。

---

## Brief A — enrich bug 详情

**目标**：现有 `scripts/out/bug_story_graph.json` 是 `--no-detail` 跑的（只有 bug_id 链接）。补全每个 bug 的 severity / status / created / resolved / closed / title / iteration_id。

**机制**：`bug_story_graph.py` 已经有 detail-enrichment 代码（`bug_cache` + `get_bug_detail`，`--no-detail` 只是跳过）。**直接不带 `--no-detail` 重跑即可**：
```bash
cd packages/story-miner && PYTHONIOENCODING=utf-8 python scripts/bug_story_graph.py
```
（约 234 次 `get_bug_detail`，1-2 分钟。`bug_cache` 自动去重。）

**输出**：覆盖 `scripts/out/bug_story_graph.json`，每个 bug 带 severity/status/时间戳。

**验收标准**：
- [ ] 234 个 bug-link 的 severity 非空率 > 95%。
- [ ] 打印 severity 分布（致命/严重/一般/…）+ status 分布（closed/resolved/new 占比）。
- [ ] 报告：top bug 磁铁 story 里，**严重级** bug 各几个（喂门禁 HIGH-block）。

---

## Brief B — story → commit（git，一期 A 的代码半边）

**目标**：给有 branch 的 story 找到它在 hc-all 子仓里的真实提交（feature 代码）。

**输入**：`~/.story-lifecycle/story.db` → `select story_key, branch, base_branch from story_project where branch is not null and branch != ''`（36 条）。17 个子仓路径（见共享上下文）。

**机制**：对每条 `(story_key, branch)`：
1. 在 17 个子仓里找该分支：`git -C <repo> rev-parse --verify <branch>`（命中即所属仓）。
2. 拿提交：`git -C <repo> log <branch> --not <base_branch> --format=... --name-only`（分支相对 base 的净提交 + 改动文件）。
3. **分支已合并删除的兜底**：`git -C <repo> log master --grep "<branch>" --oneline`（找 merge commit）→ 从 merge 取 diff。

**输出**：`scripts/out/story_commits.json`：
```json
[{"story_key":"tapd-...","branch":"feature/zzh/...","repo":"hc-message",
  "n_commits":7,"commits":[{"sha":"..","msg":"..","files":[..]}],
  "linkage":"branch"|"merge_commit"|"not_found"}]
```

**验收标准**：
- [ ] 36 条里多少 resolved（linkage != not_found），报覆盖率。
- [ ] not_found 的列出来（分支名对不上 / 已删且无 merge 痕迹）。
- [ ] 抽 2 条人工核对：commit msg/files 确实属于该 story。

**坑**：commit message 基本不引 TAPD id（实测大仓 0），别指望 `git log --grep <tapd_id>`；**靠 branch 名是唯一可靠线索**。

---

## Brief C — task_type 打标

**目标**：把 150 个 story 按受控词表（12 类）归类，填 `task_type`，供"bug-rate 按类型聚合"用。

**词表（受控，已确认；故事标题/描述归一类）**：
`credit-limit`(授信/额度/风控) · `fund-flow`(放款/还款/提现/清分/对账) · `message-notify`(短信/OTP/通知/模板) · `marketing`(营销/活动/MGM/券/免息) · `user-profile`(用户/资料/认证/隐私) · `order`(订单/交易) · `integration`(三方对接/回调) · `gateway-infra`(网关/限流/配置/调度/状态机) · `data-sql`(SQL/查询/迁移) · `frontend`(前端/admin/页面) · `deploy`(部署/上线/发版) · `debug`(排查/定位)

**机制**：LLM 分类。从 `bug_story_graph.json` 或 TAPD 拿 150 个 story 的 title+description，调 DeepSeek（config 有 key，`base_url https://api.deepseek.com`，model `deepseek-v4-pro`）。**受控选择**：prompt 给 12 类，要求只返回其一；模型若想造新类→拒绝、映射到最接近的。可批量（一次多条）省调用。

**输出**：`scripts/out/story_task_types.json`：`[{"story_key":"tapd-...","title":"..","task_type":"marketing"}]`。可选：写回 story.db 的 `story.context_json.task_type`。

**验收标准**：
- [ ] 150 个 story 全标。
- [ ] 打印 12 类分布；**0 个的类标出来**（= 盲区，呼应发散点 5）。
- [ ] 抽 5 条人工核对分类正确性。
- [ ] 跟 bug 图谱合并试算：**哪类 task_type 平均 bug 数最高**（marketing？fund-flow？）。

---

## 汇总验收（主窗口 Claude 做）

三份产物（`bug_story_graph.json` / `story_commits.json` / `story_task_types.json`）回来后：
1. **合并**：按 `story_key` join 三份 → 每 story 有 {task_type, bugs(severity), commits}。
2. **按 task_type 聚合**：bug-rate/类、严重 bug 集中的类、bug 磁铁 feature 的代码特征。
3. **覆盖率/质量核**：bug 详情完整度、commit 关联率、task_type 分布盲区、抽检正确率。
4. **产出最终 outcome 知识**：落到飞轮知识（接点 3 注入的 `knowledge_context` + 点 6 门禁的 severity→HIGH-block）。
5. **修文档**：把设计文档里"bug 自带 story_id"的错误描述改成真实机制（反向 `get_related_bugs`）。

派活时：每个窗口给本文件 + 指定它做哪个 brief（A/B/C）。产物回来 @ 主窗口汇总。

---

## Brief D / E（深入探索，第二批 · 2026-06-28）

> **比 A/B/C 更需要探索**——机制有未定项，必须**先探后做**。下面把"要探索什么"逐条写明，别跳过。A/B/C 已完成（产物在 `scripts/out/`，汇总见 `outcome_knowledge.md`）。

### Brief D — iteration_id 粗关联（bug 覆盖 20% → ~60-70%）

**背景**：精确 bug↔story（反向 `get_related_bugs`）只覆盖 20% story。但 **bug 73% 有 `iteration_id`（sprint）**，story 也有。目标：用同 iteration 撮合 bug↔story 拉宽覆盖（粗、weak 档）。

**先探索（逐条，别跳）**：
1. **iteration_id 是什么粒度**：调 TAPD iterations 接口或看 bug/story 的 iteration_id 值，确认=迭代/冲刺；拉 iteration 元数据（名称、起止）。样本：bug 的 iteration_id 集中在哪几个 sprint？
2. **覆盖重叠**：多少 bug 有 iteration_id、多少 story 有；同 iteration 里 bug↔story 能撮合多少？算"加 iteration 后关联从 20% 涨到多少"。
3. **撮合语义（最关键）**：bug.iteration_id 多半是"bug 在哪个 sprint 被发现"——该关联**同 sprint 交付的 story**，还是**前几个 sprint 交付、上线后才暴露的 story**？**先在已有精确 20% 上验证**：精确 link 的 bug/story，它们 iteration_id 关系是什么（同 sprint？差几个？），用这个定 attribution 规则。
4. **weak 纪律**：iteration 撮合 many-to-many，标 `weak:iteration`，**只做聚合**（每 sprint bug 数、每 story-cohort bug 率），不做 per-story 精确断言。

**输出**：扩 bug 图谱或新 `bug_iteration_links.json`（weak 标）；报新覆盖率 + 在精确集上规则的准确率 + per-task_type bug-rate 用宽集重算后结论变没变。

**验收**：覆盖率涨幅｜精确 20% 上规则准确率｜宽集结论是否改变 headline（marketing/credit-limit 仍最热点？）。

---

### Brief E — 无 branch 磁铁的 commit 推断（拿回 8 个大磁铁的代码）

**背景**：11 个 bug 磁铁只 3 个有 branch→commit。其余 8 个（重构hc授信节点 45 / MGM活动需求 26 / 增信提额 19 / 反欺诈人审 11 / 增信提额-人审 10 / 提现审核迁移 8 / 订单售卖 7 / 注销恢复审核迁移 7）是**老 story、无 `story_project.branch`**，代码只在 master 历史。目标：用"时间窗+语义"推断 commit（一期 B 思路，用于 story→commit）。

**先探索（逐条，别跳）**：
1. **时间窗**：story 的 TAPD 时间字段（`begin`/`due`/`created`/`modified`/`custom_field_40 预计上线`/`custom_field_190 开发开始`）哪些可靠？先看这 8 个磁铁实际填了没。定 story"实现窗"（dev_start→launch）。
2. **候选 commit 来源**：story 标题/关键词 → 猜子仓（授信→hc-limit、MGM→hc-marketing/hc-user、还款→hc-order）；在子仓 + 时间窗内 `git log master` 取候选。探索"关键词→repo"映射准不准。
3. **语义匹配**：story 标题/描述 vs commit msg（+diff）。embedding 批量 + 生成式 LLM close-call。探索哪种信号最准（关键词？文件？语义？）。
4. **验证（必做）**：在**有 branch 的 3 个磁铁**（新客7天免息/还款shopee/MGM二期）上跑推断，看能否**复现已知 commit**——方法可信后才信 8 个未知磁铁结果。
5. **confidence/tier**：推断 = `weak:semantic-time`，只收高置信，低置信不挂/标 `memory:human`。

**输出**：`story_commits_inferred.json`（8 磁铁推断 commit + 置信度 + linkage）+ 在 3 个已知磁铁上的验证结果。

**验收**：3 个已知磁铁 commit 复现率（方法可信度）｜8 磁铁高置信关联数｜抽检。

---

**派活**：D、E 各一个窗口，给本文件 + 指定 brief。强调"先探后做、逐条探索项别跳、weak 纪律、必做验证"。产物回 `scripts/out/`，@ 主窗口汇总验收。
