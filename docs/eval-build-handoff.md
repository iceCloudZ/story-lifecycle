# Eval 闭环建设 — 执行文档（opencode 实施用）

> 本文档自包含。执行者：opencode CLI。目标：为 story-lifecycle 建一套离线 eval 系统，并把 hc-aiops 的 LLM 切到 OpenCode Go 端点。每 Task 附验收命令，全部达标后由 Kimi 复核。
>
> **数据模型（用户定案）**：**三方匹配**——
> - **A. 代码提交历史**：git merge 进 master 的合并记录 = 开发完成的交付单元（交付真相）
> - **B. TAPD 需求**：TAPD API 扫出的 story 清单（需求真相，权威源）
> - **C. 本地文件**：story.db 管线记录 + `D:/hc-all/story/*` 证据目录（管线过程真相）
>
> 以 A 的 merge 交付单元为轴心，三方两两关联；**低置信度匹配一律进待确认队列，由用户人工确认后才生效**，不强行自动关联。
>
> 实施纪律：遵守本仓库根 `AGENTS.md`；最小改动、只做加法；LLM key 只走环境变量；对 hc-all 各仓库**只读 git 操作**（log/diff/show，禁止 checkout/reset/clean/fetch）；对 story.db **只读**；完成后跑 `pytest -m "not real_e2e"` 确认无回归。

## 0. 背景与资源

- **LLM 端点**：OpenCode Go 套餐，OpenAI 兼容。
  - `BASE_URL = https://opencode.ai/zen/go/v1`，`API_KEY = 环境变量 OPENCODE_API_KEY`，模型 `deepseek-v4-flash`（1M 上下文）。
- **限流（2026-08-04 实测修正）**：Go 的限制是美元等值用量（$12/5h、$30/周、$60/月，Flash 约 53 万请求/5h），**无并发数闸门，实测用量离窗口很远**。实测出现的 500/read-timeout 波次是**服务端抖动**而非撞限——应对：read timeout 设 60-90s 快速失败 + 短退避重试（抖动通常几十秒~几分钟自愈），**不要**长暂停空等。
- **并发**：允许有限并发（建议 **4-8 路**，asyncio/线程池 + semaphore），批量任务可提速数倍；但**严禁多进程重复启动同一任务**（已发生重复 score 进程互抢的事故）——任务必须断点续跑，启动前检查同类进程。
- **温度**：judge / 匹配类调用 `temperature=0`。

## 1. 已核实的关键事实（直接可用，勿重复调研）

### 1.1 A 源：交付单元（merge anchor）规模——已实测

`git log origin/master --merges` 数量：hc-user **468**、frontends/hc-admin **399**、hc-order **298**、hc-limit **153**、hc-config **46**、hc-coupon **10**。`D:/hc-all` 下还有 hc-message / hc-third-party / hc-marketing / hc-callback / hc-job / hc-audit / hc-event-tracking 等独立 `.git` 仓库，全扫预计 1500+ 交付单元。

- merge message 形如 `Merge branch 'feature/ice/ei_plan_race_nre_0728' into 'master'`，分支名可正则提取。
- 分支名含 TAPD id 的实测模式：`feature/tapd-1144381896001066735`、`feature/tapd-bug_1144381896001001116`、`feature/1064993`、`feature/zzh/1064993`、`feature/ice/loan_disclosure_1066924`。**7 位短 id 补全前缀 = `114438189600`**。
- 一个 story 常跨多仓（hc-user + hc-order + hc-admin），需按 story 聚合。

### 1.2 B 源：TAPD 取数方式——已核实

通过 hccli（ys-cli 提供）调 TAPD API：

```bash
PYTHONIOENCODING=utf-8 python "D:/agent-assets/skills/ys-cli/scripts/hccli.py" tapd get-stories \
  --workspace-id 44381896 --params '{"entity_type":"stories","status":"resolved|closed"}'
```

- **Windows 必须加 `PYTHONIOENCODING=utf-8`**，否则中文乱码。
- workspace_id 固定 `44381896`（产研项目控制中心）；story id 前缀 `114438189600`。
- 字段：id / name / status / iteration_id / owner / created / modified 等。
- ★ **`get-commit-msg` 子命令**：TAPD 侧可能已记录 story 关联的代码提交（GitLab 集成）——若可用，这是 A↔B 最高置信度的官方关联，优先试。
- 其他可用：`get-iterations`（迭代时间窗，辅助过滤）、`get-story-count`。
- 拉全量时按状态/迭代分页，结果落本地缓存文件，避免重复打 API。

### 1.3 C 源：本地文件——已核实

- **story.db**：`C:/Users/zzh58/.story-lifecycle/story.db`（只读）。
  - `story` 表（223 行，209 completed）：`story_key, title, workspace, profile, status, tapd_*, is_test, deleted_at`。
  - `gate_result` 中 `gate_name='branch_bound'` PASS 的 26 行，detail JSON 内含精确分支名 + commit hash，实测样例：`"evidence_ref": "hc-user:1528cc41, hc-order:4232e088, hc-admin:5154bdd"`、`"evidence": {"branches": [{"project": "hc-use...", "branch": "feature/ice/..."}]}`。
  - `story_change_item.evidence_ref` 常含 `commit <hash>`；列名里没有 `story_id`，先 `.schema story_change_item` 看结构。
  - detail 中文有 GBK mojibake，但 repo/分支名/hash 是 ASCII 可正常解析。
- **证据目录** `D:/hc-all/story/<story-id>-<slug>/`（65 个）：`PRD.md / research.md / spec.md / plan.md / test-report.md` + `.meta`，可选 `ddl.sql`、`delivery/`。
- 阶段回执：`<workspace>/.story/done/<story_key>/{design,build,verify}.json`。
- spec 评分标准：`D:/hc-all/docs/spec-template.md`（必填 Release 章节：SQL 变更、Nacos 配置变更、验收测试、验收计划、大表名单）。

### 1.4 LLM 客户端（复用，不要新写 HTTP 封装）

`packages/story-lifecycle/src/story_lifecycle/infra/llm_client.py`：OpenAI 兼容薄封装；env 配置（:256-260）`STORY_LLM_API_KEY / STORY_LLM_BASE_URL / STORY_LLM_MODEL`；方法 `invoke / invoke_json / invoke_structured(Pydantic)`；单例 `get_llm()`。接 Go 端点 = 设这三个 env。

### 1.5 回放基础设施（Task 4 用）

`packages/testing/src/testing/harness.py:run_real_story()` + `workspace.py:reset_workspace()` + `asserters.py`；profile 参考 `entry/profiles/headless-smoke.yaml`（`execution_mode: headless`、`confirm: false`）；adapter 支持 opencode CLI；**`STORY_HOME` 是隔离开关**（回放设临时目录）；编程驱动参考仓库根 `tmp_drive_1065570.py`。

## 2. Task 0 — hc-aiops 切换 LLM 到 Go 端点

1. 读 `D:/hc-all/hc-aiops/analyzer/` 的 LLM 调用代码，确认/补齐 `base_url` 可配。
2. 改 `D:/hc-all/hc-aiops/config.yaml`（约 :112-114）：
   ```yaml
   llm:
     enabled: ${OPENCODE_API_KEY:+true}
     base_url: ${OPENCODE_GO_BASE_URL:-https://opencode.ai/zen/go/v1}
     api_key: "${OPENCODE_API_KEY}"
     model: ${LLM_MODEL:-deepseek-v4-flash}
   ```
3. 验证：对一条历史错误跑一次诊断，无 401/403/持续 429；常驻服务则重启观察一轮钉钉告警带 AI 结论。

**验收**：config diff 如上；日志出现成功 LLM 调用；≥1 条带诊断结论的告警。

## 3. Task 1 — 三方数据采集 + 匹配 ★ 数据基石

### 3.1 建包 `packages/eval`（参考 `packages/testing` 惯例）

```
packages/eval/
  pyproject.toml          # story-lifecycle(workspace), pydantic, httpx, click, pyyaml
  src/eval/
    cli.py                # eval index / tapd-scan / link / review-apply / extract / score / scan-all / replay / report
    gitindex.py  tapdscan.py  linker.py  dataset.py
    judges.py  baseline.py  replay.py  report.py
  dataset/  results/      # 均 gitignore
```

根 `pyproject.toml` 注册；根 `.gitignore` 加 `packages/eval/dataset/`、`packages/eval/results/`。

### 3.2 `gitindex.py` — A 源采集

- 仓库清单：扫 `D:/hc-all/*/` 含 `.git` 的目录 + `D:/hc-all/frontends/hc-admin`。**不 fetch**，直接用本地 `origin/master` 引用。
- 每仓（只读）：
  - `git log origin/master --merges --format=%H|%aI|%an|%s` → merge 单元；`%s` 正则 `Merge branch '([^']+)'` 提取分支名。
  - 分支提交：`git log <merge>^1..<merge>^2 --format=%H|%aI|%s`；`^2` 不存在的退化情况跳过并记录。
  - diffstat：`git diff --shortstat <merge>^1 <merge>`。
  - 非 merge 直接提交可选记为 `kind=direct` singleton 单元。
- 输出 `dataset/deliveries.jsonl`：`{repo, merge_hash, branch, merged_at, author, commits[], diffstat, kind}`。

**验收**：≥1300 行；抽 3 个 merge 的 commits 与手工 `git log` 一致。

### 3.3 `tapdscan.py` — B 源采集

- 用 1.2 命令分页拉 workspace `44381896` 的 stories（至少 resolved|closed；量可控则连 in_progress 一起拉，便于看「有需求无交付」）。
- 试 `get-commit-msg`：若返回 story 关联的 commit 信息，存为 A↔B 官方关联种子。
- 输出 `dataset/tapd_stories.jsonl`：`{tapd_id, name, status, iteration_id, owner, created, modified}` + 可选 `dataset/tapd_commits.jsonl`。

**验收**：拉取数 ≥ story.db 完成数（209）；文件含中文无乱码。

### 3.4 `linker.py` — 三方匹配（信号按优先级，低置信进人工队列）

**A↔B 信号（merge ↔ TAPD story）**：
1. TAPD `get-commit-msg` 官方关联（若 3.3 拿到）→ `confidence=official`。
2. 分支名含 id（正则 `tapd-(?:bug_)?(\d{18})`、`(?:feature/(?:\w+/)?|_)(\d{7})\b`，短 id 补前缀 `114438189600`）→ `high`。
3. **LLM 模糊匹配**（Flash，批量，并发 4-8 路）：分支 slug + commit messages 摘要 vs 候选 TAPD story name/description。候选收窄手段：merge 时间 ±45 天 + 迭代归属过滤；**git author ↔ TAPD owner 映射**（如 `zzh58` ↔ `赵子豪`，映射表从 high 置信关联里自动学习 + 可手工补充）。输出 `tapd_id + confidence`；**≥0.8 → `medium`，0.5-0.8 → 待确认队列，<0.5 → 不关联**。明显无对应 story 的（chore/版本号/hotfix 类）允许 LLM 返回 null，不算漏匹配。

### 3.4.1 `link-mine` + `verify-links` — 个人关联深化 + 独立复核（2026-08-05 新增）

**背景**：全量关联率对个人 merge 太稀疏（664 个只有 ~45 个链上 story），加了 `eval link-mine` 把候选收窄到「TAPD owner=赵子豪」+ merge ±90 天，0.8 阈值自动关联。但**单阶段 LLM 自动关联不可信**（实测 215 条 llm_mine_high 里 ~43% 被独立复核判为 unrelated/uncertain），因此必须加独立 verify pass：

- `eval link-mine`：只在我的 TAPD story（owner 含赵子豪）里匹配，时间窗 ±90 天，关键词预排序 top 15 候选再让 LLM 判。≥0.8 → `llm_mine_high` 自动关联（仍须过 verify）；0.5-0.8 → 待确认队列。
- `eval verify-links`：对 `links_pending_review.md` 全部行 + `stories_matched.jsonl` 里全部 `llm_mine_high` 逐条**无锚定**复核（并发 8，默认 DeepSeek 官方端点 `dataset/.env.deepseek`）：
  - 输入：TAPD name + desc（去 HTML） vs merge 分支名 + commit subjects + 关键 diff（≤30k token）。
  - **prompt 不得包含之前的置信度/关联结论/疑似错链标记**（无锚定，避免 self-consistent 幻觉）。
  - 输出 `related / unrelated / uncertain` + 一句话理由。
  - 结果落 `dataset/verify_links_<date>.jsonl`，不自动执行。
- 生成**分层抽样清单** `dataset/verify_links_sample_<date>.md`（related/unrelated/uncertain 各抽 7 条）→ 用户人工校准。
- `eval apply-verify <抽样清单>`：用户校准后执行分级——
  - **related → accept**：llm_mine_high 保留为 high（`link_method=verify_related`）；pending 行加入 stories_matched。
  - **unrelated → reject**：从 stories_matched / pending 移除。
  - **uncertain → 留队列**：llm_mine_high 降级回待确认队列。
- `link-mine --verify`：跑完 link-mine 自动接 verify-links（固化流程）。

**原则**：**verified-related 才允许转 high 关联**。单阶段 LLM 匹配（任何阈值）只产出候选，最终 high 必须经过独立 verify 通过（或人工 review-apply）。

**人工标记语义（2026-08-05 补）**：
- `human_confirmed: true`：人工确认的链接，**只免疫自动规则**（日期守卫、`_is_suspected_wrong_link` 疑似错链追加等），不代表永久正确。
- 出现**新证据**（如 ref-fetch 抓到正文、diff 复核发现错链）时，由**人工发起再裁决**，自动流程不得自行改判已人工标记的链接。
- 人工改判（如确认错链）时：stories_matched 的 delivery 标 `rejected: true` + `human_recalibrated: true` + 清除 `human_confirmed`，`confidence` 改 `rejected`（load_match_index 不再收录）；merge_scores 的 conformance 分数**保留**作 drift 证据，但 `tapd_id` 清空使该 merge 回无关联池。`_is_suspected_wrong_link` 同样跳过 `human_recalibrated`，避免改判后又被自动追加进待审队列。

**A↔C 信号（merge ↔ story.db）**：
4. commit hash 精确匹配种子（branch_bound `hc-user:1528cc41` 式、story_change_item 的 hash）→ `high`。
5. 分支名精确匹配种子（branch_bound evidence.branches）→ `high`。

**B↔C 信号**：story.db `story.tapd_*` 字段直接对 tapd_id → `high`；story_key 数字部分 = TAPD 短 id 的直接对上 → `high`。

**三方汇合**：以 tapd_id 为主键建统一实体 `dataset/stories_matched.jsonl`：
```json
{"tapd_id":"1144381896001065570","name":"...","story_key":"1065570",
 "deliveries":[{"repo":"hc-user","merge_hash":"...","branch":"...","link_method":"seed_hash","confidence":"high"}],
 "evidence_dir":"D:/hc-all/story/1065570-联系人姓名校验",
 "link_summary":{"A_B":"high","A_C":"high","B_C":"high"}}
```

**待确认队列**：所有 medium/冲突/多候选项写 `dataset/links_pending_review.md`（表格：merge 信息 / 候选 story / LLM 理由 / 建议），并**停下来通知用户人工确认**；用户在表格上标注后跑 `eval review-apply` 合并进正式结果。

**覆盖率报告** `dataset/coverage_report.md`：A/B/C 各自总数、A∩B / A∩C / B∩C / A∩B∩C 数量、孤儿清单（有 TAPD 无交付、有交付无 story、story.db 有记录 TAPD 查无）。

**全量原则（用户定案）**：匹配**不区分 git 作者**——同事的 merge、同事的 TAPD story 全部纳入。gitindex 已记录 `author` 字段，TAPD 扫的是全 workspace。gold 集因此从「管线跑过的 ~209 个」扩到「全团队有交付的数百个」；管线内（有 C 源文档）vs 管线外（无 spec/plan）的对比本身就是分析维度，按 author 维度的 DeliveryScore 也可正常使用。

**验收**：`eval index && eval tapd-scan && eval link` 跑完；A∩B high/official 关联 ≥60；覆盖率报告生成；待确认队列已提交用户并应用确认结果。

## 4. Task 2 — story 维度 eval 数据集

`dataset.py`：以匹配后的统一实体为轴，落盘评分材料：

- **A**：该 story 全部 merge 单元的 commits + diffstat；core 集额外落 diff 全文到 `dataset/<tapd_id>/diffs/<repo>_<merge>.diff`。
- **C**：证据目录文档（PRD/spec/plan/test-report/research + .meta）+ `.story/done` 回执 + gate_result 记录。
- **B**：TAPD name/status/iteration/owner。
- `manifest.json`：三方字段 + 关联方法与置信度 + core 标记。入选门槛：**A∩B（有交付有需求）或 C 有 PRD+spec**；`core=true` = spec+plan+test-report + ≥1 个 high/official 交付单元。

**验收**：入选 ≥50（A∩B 的 ≥40）；core ≥15；抽 2 个 manifest 人工核对。

## 5. Task 3 — LLM judges + 历史基线

### 5.1 `judges.py`（复用 1.4；`EVAL_LLM_*` 可覆盖 `STORY_LLM_*`）

Pydantic schema（每维度 1-5 + `findings` + `summary`），中文 prompt、reference-based：

- `SpecScore`：`completeness`（对照 PRD/TAPD 需求描述）、`template_compliance`（spec-template.md 必填章节）、`acceptability`。
- `PlanScore`：`specificity`、`spec_alignment`、`verifiability`。
- `ConformanceScore` ★核心：**merge diff（真实交付）vs 需求参照物**——`alignment` / `coverage` / `scope_drift`。参照物优先级：C 源 spec > C 源 PRD > **B 源 TAPD 需求描述**（管线外 story 没有 spec/PRD，用 TAPD 描述兜底，需在 tapdscan 时一并拉取 description 字段）；评分输出须标注实际用的参照物类型。diff 大按文件分批 + 汇总。
- `DeliveryScore`：commit message 质量、提交粒度、revert/fixup 返工迹象。
- `MergeSummary`（无关联 merge 的兜底）：该 merge **实际做了什么**的一段语义摘要（从 commits + diffstat + 关键 diff 归纳），存回索引——反哺第二轮模糊关联（拿摘要去对 TAPD name 比裸 commit message 准得多），也是孤儿分析的材料。

调用纪律：`temperature=0`、有限并发 4-8 路（semaphore）、read timeout 60-90s 快速失败 + 短退避重试 2 次、仍失败记 error 不中断。

### 5.2 `baseline.py`

core 集全量打分 → `results/baseline_<YYYYMMDD>.json` + `.md`（分布、低分 Top10 附 findings、自洽性：随机 10 个评 2 次分差 ≤1 占比 >80%、**spec-代码漂移 case 列表**）。

**验收**：baseline 生成；自洽性 >80%。

### 5.3 全量扫描模式（用户定案：全量 merge 都跑）

`eval scan-all`：对 `deliveries.jsonl` **全部 ~1500 个 merge** 逐个评分，不只是 core 集。

- **评什么**（按关联状态分档）：
  - 有关联 story（high/official/已确认）：`ConformanceScore`（参照物 spec > PRD > TAPD 描述）+ `DeliveryScore`。
  - 无关联 story：`MergeSummary` + `DeliveryScore`；摘要写回索引后**自动跑第二轮模糊关联**，新命中的进待确认队列给用户。
- **diff 预算控制**：单 merge 送审内容设上限（diffstat 全文 + 按 churn 排序的 top 文件 diff，总量截断 ~80k token）；超限的在结果里标 `truncated: true`。
- **断点续跑（必须）**：结果按 `merge_hash` 增量落 `results/merge_scores.jsonl`（每完成一个追加一行），重跑自动跳过已完成的；支持 `--limit N` 分批。总量估 60-90M token、7000+ 次调用，**并发 4-8 路下预计数小时跑完**（额度充足，见 §0 实测修正）；启动前检查无同类进程在跑。
- **进度与 ETA**：每 50 个输出一次进度（已完成/剩余/已耗 token/按当前速率的 ETA）；连续失败率突增（服务端抖动）时打印提示并短暂退避后自动恢复。
- **报告** `results/full_scan_<YYYYMMDD>.md`：全量分布（各维度均分/直方图）、低分 drift case Top 20（附 findings 与 story 链接）、按 repo / 按 author / 管线内 vs 管线外 的对比维度、truncated 比例。

**验收**：断点续跑验证（跑到一半中断，重跑不重复已完成的 merge）；全量完成后报告生成，token 实耗与估算同量级（<150M）。

## 6. Task 4 — 回放 harness + 回归 diff

- 回放集：core 集中挑 **5-8 个轻量 story**（交付在 `story-lifecycle` 本仓/Python 项目优先，排除 hc-all Java 重构建）→ `packages/eval/replay_set.json`。
- `replay.py`：隔离 `STORY_HOME` → `reset_workspace()` → profile `eval-replay.yaml`（复制 `headless-smoke.yaml`，`cli: opencode`）→ `run_real_story()`（输入 gold PRD）→ 收 artifacts 到 `results/replay_<date>/<story_key>/`。管线 LLM 同走 Go 端点。单 story 30min 熔断，失败记录继续。
- `report.py`：judges 打分 vs gold 基线分 + vs 上次回放分 → `results/regression_<YYYYMMDD>.md`，跌 >1 标 🔴 附 findings，执行失败单列不计回归。

**验收**：1 个 story 全链路跑通 → 全集一轮无持续 429。**真实驱动 opencode CLI 跑代码，执行前先向用户确认时机。**

## 7. Task 5 — 文档固化

1. `packages/eval/README.md`：命令用法、env 前置、限流说明、三方数据模型图。
2. 根 `AGENTS.md`：eval 层定位（离线 judge 层）；「改 `infra/prompts/` 或 `orchestrator/evaluation/` 后跑 `eval replay`」约定；**数据基石 = 三方匹配（merge 交付单元 × TAPD × 本地文件）**。
3. README 记录首轮实测 token 消耗。

## 8. Phase 2 — 常驻循环（把额度转成持续价值）

> 前提：Task 1-3 跑通、额度实测充足（§0）。三个任务按序推进，全部前台跑在 opencode 会话里。

### Task 6 — 增量常驻：新 merge 自动评分 + 每日变更体检

`eval watch`：
- 记录每仓上次扫描位置（`results/watch_checkpoint.json`），每次运行只处理**新出现的 merge**：`git log <last_merge>..origin/master --merges`。
- 新 merge 走既有链路：link（新 merge 对 TAPD）→ ConformanceScore + DeliveryScore → 追加 `merge_scores.jsonl`。
- 生成当日摘要 `results/daily_digest_<date>.md`：新交付清单、各 story conformance 分、风险标记（费用配置/DDL/大 diff/低分 drift）。
- 钉钉推送做成 `--push-dingtalk` 显式开关（webhook 走环境变量），默认只落文件。
- 调度：Windows 任务计划每天 1-2 次；必须先验证单实例运行（检查同类进程）。

### Task 7 — transcript 全量蒸馏回灌

- 数据源：`D:/github/agent-transcript-miner/data/transcripts.db` + story-miner 的 transcripts（本地，含 PII 不出本机）。
- `eval distill`（或在 story-miner 加脚本）：Flash 批量过历史会话，蒸馏三类产物：
  1. **失败模式**（agent 卡住/返工/误解需求的 pattern）→ `results/distill/failure_modes.md`
  2. **约束/坑**（项目级规则，如「test 分支不能当基线」）→ 回灌 `D:/hc-all/.story/knowledge/` 与根 AGENTS.md 候选
  3. **playbook**（某类任务的成功路径）→ story-lifecycle `infra/prompts/` 的改进输入
- 分批跑，每批结束输出摘要；产出全部进待人工 review 文件，不自动合入 prompt。

### Task 8 — 自治改进循环（主引擎，80% 额度去向）

目标：用 eval 低分 case 驱动 story-lifecycle 自我修复。协议：

1. **选 case**：从 `baseline_*.json` / `full_scan_*.md` 取低分 Top N（或 judge error 集中的模式），定位对应的管线环节（prompt 模板 / gate 逻辑 / context 组装）。
2. **修复尝试**：opencode 会话在 story-lifecycle 仓库**开分支**做最小修复。
3. **验证**：跑回放集（Task 4 harness，5-8 个轻量 story）→ judges 对比修复前后分数。
4. **取舍**：分数提升保留并记录；无提升/回退则 revert。**绝不自动合并 master**。
5. **日志**：每轮迭代写 `results/improvement_log.jsonl`（case / 假设 / 改动 / 前后分 / 结论）。

护栏：
- 每天上限 20 轮迭代或 100M token（先到先停）。
- 改动范围限 story-lifecycle 仓库；hc-all 业务代码禁止碰。
- 每天结束产出迭代摘要给用户人工 review，保留/合并由用户定。

### Task 9（可选扩展）— 回放集扩量

回放集从 5-8 扩到 30-50 个 story（仍是 Python/轻量优先），prompt 模板每次改动后全量回放当回归测试。

## 9. 完成定义（DoD）

- [ ] Task 0：hc-aiops analyzer 走 Go 端点产出 ≥1 条带 AI 诊断的告警
- [ ] Task 1：`deliveries.jsonl` ≥1300 行；TAPD 拉取 ≥209；A∩B high/official ≥60；**待确认队列经用户确认后应用**；三方覆盖率报告生成
- [ ] Task 2：入选 ≥50 / core ≥15；dataset/results 不进 git
- [ ] Task 3：baseline 报告，自洽性 >80%；**`eval scan-all` 全量 ~1500 个 merge 跑完，断点续跑验证通过，`full_scan_*.md` 生成**
- [ ] Task 4：≥5 个 story 回放全链路 + `regression_*.md`
- [ ] Task 5：README + AGENTS.md + token 实测
- [ ] Task 6：`eval watch` 增量评分跑通，daily digest 产出
- [ ] Task 7：transcript 蒸馏三产物落盘（人工 review 后回灌）
- [ ] Task 8：自治改进循环跑通 ≥3 轮完整迭代（选 case→修复→回放验证→取舍→日志）
- [ ] `pytest -m "not real_e2e"` 全绿
