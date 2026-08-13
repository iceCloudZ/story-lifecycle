# 迭代4 双线合并视图 + 101 数据管线设计

> 本文件合并两份文档：`docs/b-line-101-handover.md`（B 线 path A 迁移）+ `packages/story-lifecycle/docs/test-runs/SERVE-ROBUSTNESS-2026-08-11.md`（path B / serve 轨），并把「历史 story + 代码清洗后上 101」升级为**持续数据管线设计**。
> 版本 v1.0（2026-08-13）｜ 决策已拍板：数据=历史+新上线持续管线 ／ 代码=近一年 bundle ／ 文档=本文件一份 ／ 触发=手动跑稳再挂定时。
> 状态：本地 runner 已停，迁移执行中。

## 1. 主线（一句话）

迭代 4 回答两个问题：**「裁判听内容还是听证据」（A 线，结论已出：系统性保守，议题升级迭代5）** 和 **「回测能不能在生产路径上跑」（B 线）**。B 线拆两条互补执行轨——**path A**（in-process 沙箱，本地 Windows 跑 50 条全链路回放）与 **path B**（serve 驱动，101 服务器跑 ui-replay/ui-full），两轨同 gold、同 judge 三元组（Go 端点 + deepseek-v4-flash），在 **101 上同机合并**跑出 A vs B 差分。从今往后，101 上的回测输入不再靠一次性交接，而由**本地→101 的清洗数据管线**持续供给（历史 + 新上线 story + 业务代码）。

## 2. 两线合并视图（时间线）

| 时间 | 线 | 事件 |
|---|---|---|
| 08-04 ~ 08-12 | 共同地基 | eval-journey：历史交付物 → 评分数据集 → 回放测试 → 修复 → 快照回归；judge 三元组、测试金字塔、三轮修复（含迭代3 裁判统一） |
| 08-11 夜 ~ 08-12 晨 | path B | serve 崩 5/6 复现（design→implement 转换点）→ 6 轮排查（D1–D8）→ **auditd 抓到真凶：sshd 会话拆除 SIGKILL**（462/462 事件全是 sshd）→ 永久 `story-serve.service`（脱 sshd + Restart + linger），9h+ 零崩 |
| 08-12 | path B | 三个真 bug：headless opencode 泄漏（`kill_headless` + `start_new_session`，含 killpg 误杀 serve 的回归，服务器实测才暴露）、`POLL_TIMEOUT` 600→1500（flash 8~15min 方差伪停滞）、启动自检。**全流程闭环**：ui-full auto-advance 过 confirm-gate → design→implement→verify→done 全自动跑通；**prompt 迭代闭环**：template_compliance 2.4→3.8（+58%） |
| 08-12 | path A | 50 条选样核验通过（10 evidence + 19 story_refs + 21 v2-qualified，seed 42，零重叠）；修三坑：PRD 短 id bug（`[-7:]`→`[12:]`）、story_refs 钉钉 UI 噪音清洗词表、confirm-gate 卡死（pipeline_replay 加 auto-advance）；看门狗 900→3600s。本地真完成 5 条、25 条 stall（13 薄 PRD no_artifacts + 7 尾闸 + 5 watchdog）。**runner 被 Windows 外部清理杀 3 次** → 决定迁 101 |
| 08-13 | 汇合 | worktree 两层清理闭环（磁盘 slug dir + 注册 prune，eval 跑一轮 0 积累）；runner 落位 eval 包（c5c609a4）；本地 runner 停（单 runner 切换）；本管线方案落地 |

**两轨分工与合并价值**：path A 回答「真实 agent 能不能把 50 条历史需求在沙箱跑完」（in-process，无 serve 调度层）；path B 回答「生产 serve 调度层下同样能跑」（同 gold 同 judge，不测孤儿裁判）。同机跑后 `eval diff` 量化 serve 调度层影响——第一份结论：**serve ≈ in-process**。

**纪律（两轨共用，逐条有效）**：单 runner（一条故事绝不两边同时跑）／ judge 三元组 Go 端点 only，放量前 pre-flight 打印 base_url ／ 跑完 grep api.deepseek.com 必须 0 命中 ／ 连续 3 条 infra 失败自动暂停 ／ **101 只收管线产出，绝不自己拉内网**。

## 3. 101 数据管线设计（本轮拍板）

### 3.1 定位与边界

- **持续清洗导出管线**：本地（有内网）= 源，101 = 目的地，单向。批式文件包（manifest + tar/bundle + sha256），非流式。
- **纪律升级**：原「101 绝不碰内网」强化为「**101 只收管线洗过的数据，绝不自己拉**」——bundle clone 出来无 origin，天然满足。
- **101 落点**：`~/story-lifecycle/packages/eval/`（与本地同构，代码/脚本走 git，dataset/results/sandbox 整体 gitignored、随数据包 scp）。偏差说明：交接文档原建议 `~/story-eval/`，且误记「b_batch50 清单 git main 已有」（实际 `packages/eval/dataset/` 被根 .gitignore 忽略，清单随包走）；101 已有 repo clone + editable 安装，同构落点零配置；`EVAL_PACKAGE_ROOT` 环境变量留作迁移后路。

### 3.2 story 管线（判定源 / 增量 / 清洗 / 形态）

**判定源（新上线 story 怎么发现）——三链合并去重，每条带来源标记：**

1. **代码链（最准，待启用为主）**：14 个业务仓（`D:/hc-all/<proj>/`，各自独立 .git）新增提交 → match index（332 条 `(项目, commit_sha)`→tapd_id 映射）反查 → evidence 目录取 spec/PRD 全文。**依赖上线 git 约定（tag/分支模式）——约定待用户提供，之前不启用为主判定**。
2. **TAPD 交付链（v1 主判定）**：本地 6309 条 tapd recs 的交付时间 > 上次导出 marker → 新交付 story 清单。交付时间即事实上的「上线」。
3. **miner 链（兜底）**：`python -m miner.store --since-days N` 新转录 → story。

**增量 marker**：每仓 last-synced commit sha + tapd 最大交付时间 + miner 行号，下批从 marker 往后扫。

**清洗**：已有三件套沿用（短 id `[12:]` 修复 + 钉钉噪音词表 + GBK→UTF-8 统一）；新增剥密规则待 gitleaks 摸底后定稿（见 3.3）。

**形态**：批式导出 = manifest（条目/来源/sha256/清洗规则版本）+ tar + 本地 ledger；`pushed_to_101` 单字段升级为 ledger 条目（见 3.6）。

**gold 在源头构造（迁移首日实测教训）**：101 无参照物链（evidence 快照/match index/story_refs），`ensure_gold_prd` 懒构造必退化薄 PRD → 批量 stall。管线纪律：**gold PRD 一律在本地（源头）构造完成后随包出**，101 只收成品。

### 3.3 代码管线（bundle 机制，已拍板）

- **范围**：14 个业务仓（hc-config/hc-limit/hc-admin/hc-user/hc-order/hc-message/hc-audit/hc-callback/hc-third-party/ys-marketing/hc-marketing/ys-crowd/hc-aiops/hc-job），**近一年历史**（已拍板）。
- **机制 = bundle（邮包式）**：

  ```bash
  # 首次（本地，近一年全量）
  git -C /d/hc-all/hc-order bundle create hc-order-1y.bundle --since='1 year ago' --all
  # 增量（每次管线跑，marker = 本地 refs/b101-marker）
  git -C /d/hc-all/hc-order bundle create hc-order-inc.bundle <marker>..<head>
  scp hc-order-inc.bundle 101:~/story-lifecycle/packages/eval/bundles/
  # 101 侧：首次 clone（无 origin，天然断内网）；增量 fetch（bundle 可直接当 remote）
  git clone hc-order-1y.bundle repos/hc-order
  git -C repos/hc-order fetch ../bundles/hc-order-inc.bundle <head>:main
  ```

  选 bundle 而非 push --mirror 的理由：传输物是**单个文件**（sha 完整性、断点续传、失败重传、bundle 留档即账本）、101 侧零配置（无需 bare 仓/git-receive-pack）、与 story 管线的「文件包 + 审计字段」同一心智。
- **剥密流程**：先 gitleaks/trufflehog 对 14 仓摸底 → 出报告 → `git filter-repo --invert-paths` 一次性重写剥密（sha 全变，marker 以重写仓为准）→ 近一年截断 bundle。**先摸底后定强度，不拍脑袋剥**。
- **旧镜像替换**：101 上 path B 时代的 `repos/hc-order`、`repos/hc-limit`（未清洗版）换为清洗版重新导入，统一「只收管线产出」纪律。

### 3.4 101 侧 ingest

`eval ingest`（101）：校验 manifest sha256 → 落位（dataset/gold/bundles）→ ledger 回执（成功/失败条目）→ 报告。任何校验失败该批不落位。

### 3.5 触发与演进

- v1：**手动 batch**（export-batch → scp → ingest），跑稳后再挂定时（本地 Windows 计划任务）。
- 目标态：与「nightly 常驻化」合并——B 线主跑完后每晚批导出新 story；代码仓按周/按需同步。
- 管线入口落 `packages/eval`：本地 `eval export-batch` + 101 `eval ingest`。

### 3.6 记账（ledger，双向对账）

每批一个 batch_id：日期 / 条目清单 / sha256 / 来源链标记 / 清洗规则版本 / 传输方与接收方回执。本地写 ledger，101 ingest 回执对账——`b_batch50_20260812.json` 的 `pushed_to_101` 字段（本次写 "2026-08-13"）是 ledger 的第一条。

## 4. 当前状态与执行计划

- [x] 本地 runner + b_watch 停，进程清零（单 runner 纪律满足）
- [x] runner 脚本入库（c5c609a4，101 走 git 不需另传）
- [ ] runner 路径可移植 patch（`__file__` 推导 + env 覆盖，101 为 Linux）
- [x] `b_batch50` 写 `pushed_to_101` 审计字段（已写入；dataset/ gitignored 不入库，随包 scp）
- [ ] scp：b_batch50 清单 + 断点 jsonl + gold ×52 → 101
- [x] 101：git pull → pre-flight（打印 Go 端点）→ `--only` 首条 → 断点确认（5 ok 跳过）→ systemd user unit 放量（`b-line-runner.service` 已起，45 条进行中）
- [x] 管线 v1：gitleaks 摸底（7 仓干净 6 仓 62 处）→ 剥密（删 6 类路径 + regex 替换 GOCSPX/developerToken）→ graft 1y 截断 → 13/13 bundle 落 101 ingest 回执（旧镜像 .pre-scrub.bak 保留；hc-admin 源仓缺失待查）
- [ ] export-batch / ingest 脚本落位 + 手动跑通 → 跑稳挂定时
- [ ] B 线跑完：b_final_stats 口径 append `results/iteration4_20260812.md` §5 + `results/nightly/b_line_final_2026081X.md` 回流

## 5. 验收清单

- [x] 101 上首条（`--only`）跑通，pre-flight 确认 Go 端点（1034681：design+verify 全链跑通、行落盘、零进程泄漏；瑕疵=verify judge LLM 11:12-11:14 瞬态失败 3 次→active_stall，同配置 11:50 实测 9.4s 通过，判端点抖动，放量后留意频率）
- [x] 断点确认：已完成 5 条被跳过，剩余 45 续跑（`[B] 断点: 已完成 5，剩余 45`）
- [x] 本地 runner + b_watch 已停（单 runner）
- [x] `pushed_to_101` 审计字段已写（随数据包 scp，dataset/ gitignored）
- [x] systemd 托管生效（`b-line-runner.service` active+enabled，Restart=on-failure，心跳=`results/b_line_20260812.out`）
- [x] 首个业务仓清洗 bundle 落 101 + ingest 回执（管线链路闭环：13/13，receipts 见 101 `results/b_code_receipts.jsonl`）

## 6. 开放项

- **上线 git 约定**（tag/分支模式）待用户提供 → 代码链升为主判定
- **hc-admin 仓缺失**：match index 有 35 条 (hc-admin, commit) 映射但 D:/hc-all 下无此仓——待用户确认去向（改名/合并/独立仓）
- gitleaks 报告 → 剥密规则定稿（v1 已按报告执行；后续新仓先扫后传）
- 管线 v1 两个实操坑已修并留痕：replace-text 必须 `regex:` 前缀；`bundle --since=1y` 会产生 prerequisite（空仓 clone 失败）→ 改 graft+filter-repo 烙进截断
- 迭代 5 议题池：A 线保守偏向（conformance 措辞与分数分离）/ 薄 PRD agent 行为定义 / 看门狗分档 / declare 契约归因 / nightly 常驻化
