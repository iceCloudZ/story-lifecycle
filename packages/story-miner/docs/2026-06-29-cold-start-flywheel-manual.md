# 冷启动飞轮 — 使用手册 & 总览

> 2026-06-29 · 帮你理清最近集中做的这套：**把 hc-all 的历史挖成知识 → 注入 prompt → AI 用上 → 产出又回流成新知识**。

## 一句话

**挖（transcript + TAPD + git）→ 知识（bug-prone / playbook / 基础）→ 注入 prompt → AI 用上 → 产出回流**。飞轮越转越厚，全自动/半自动两套流程共用同一套知识。

## 总览图

```
                hc-all（代码 + agent transcripts + TAPD story/bug）
   ┌──────────────────────┼────────────────────────┐
   ▼                      ▼                        ▼
 结果轴挖掘              过程轴挖掘               基础知识（已有，06-02）
 A bug↔story(反向)       F playbook/failure        bootstrap-prompt.md
 C task_type(12类)       (transcripts.db)          manifest/scenarios/indexes/graph
 B/E story→commit
 I bug-prone/cycle-time
 D iteration兜底(weak)
   │                      │                        │（待接注入）
   └──────────┬───────────┘                        │
              ▼                                    │
      knowledge_provider（按 task_type 拼知识）◄───┘
              │
              ▼  prompt_sections（共享 helper，两套流程公用）
   ┌──────────┴──────────────┐
   ▼                         ▼
 _build_cli_prompt        _render_prompt            release_prompt.py（半自动 web，待接）
 (全自动 agent-mode)      (半自动 dry-run 预览)
   │                         │
   └────────────┬────────────┘
                ▼
     story: design → build → verify
       （注入：高风险文件 / cycle-time / bug 磁铁 / 建议）
                ▼
          AI (claude) 干活   ◄─ G-run 实证：AI 真用上了（引用 bug_weight、逐项评估回归）
                │
   ├─ verify gate(H)：HIGH findings → 反思式 repair → 升级人工
   ├─ 硬关联(J)：branch 带 {story_key} → commit 可反查
                ▼ 产出
     新 transcript / 新 bug ──(miner refresh)──► 回到顶部，飞轮转下一圈
```

## 我们做了什么（按模块）

| 模块 | 干啥 | 脚本/文件 | 状态 |
|---|---|---|---|
| **A** bug↔story | 反向 `get_related_bugs`（正向 story_id 全空） | `scripts/bug_story_graph.py` | ✅ |
| **C** task_type | 12 类受控词表打标 | `scripts/classify_story_task_type.py` | ✅ |
| **B/E** story→commit | branch + 无 branch 磁铁推断 | `scripts/story_commits.py` / `infer_bug_magnet_commits.py` | ✅ |
| **D** iteration 兜底 | 同 sprint 粗关联（weak，精度≈0，只 sprint 级） | `scripts/bug_iteration_links.py` | ✅ weak |
| **I** 结果轴二期 | bug-prone 文件 / cycle-time / churn | `scripts/result_axis_phase2.py` | ✅ |
| **F** 过程轴 | per-task_type playbook/failure | `scripts/task_type_playbooks.py` | ✅ |
| **G** 注入 | `knowledge_provider` + `prompt_sections` 共享 helper | `context_providers/` + `orchestrator/prompt_sections.py` | ✅ G-run 验证 AI 用上 |
| **H** 门禁 | verify HIGH findings → repair round | `orchestrator/gate.py` / `quality.py` | ✅ wired |
| **J** 硬关联 | branch_rule 带 `{story_key}` + commit-msg hook | `profiles/` + `scripts/install_commit_msg_hooks.py` | ✅ |
| 基础知识 | 项目结构（域/服务/表/MQ） | `bootstrap-prompt.md`（hc-all 已有 06-02 verified） | 🟡 没接注入 |

## 数据怎么流（落库）

```
transcripts.db  ← miner 采 hc-all agent 会话（sessions/events/token_usage 26666 行）
story.db        ← story-lifecycle 生命周期（story/stage_log/finding/story_project/delivery_artifact/llm_trace）
scripts/out/*.json ← 冷启动脚本产物（bug_story_graph / story_task_types / story_commits / result_axis_phase2 ...），gitignore 本地
hc-all/.story/knowledge/ ← 基础(manifest/scenarios/indexes/graph) + 过程(playbooks/failures)
```
⚠️ `story.db` 有 `~/.story-lifecycle/backups/`；**`transcripts.db` 没备份**（金矿在这，建议加定时备份）。

## 怎么用

**1. 冷启动挖矿**（一次性 / 定期重跑，产物落 `scripts/out/` + `hc-all/.story/knowledge/`）：
```bash
cd packages/story-miner
python scripts/bug_story_graph.py          # A: bug↔story + severity/time
python scripts/classify_story_task_type.py # C: 12 类 task_type
python scripts/story_commits.py            # B: branch→commit
python scripts/infer_bug_magnet_commits.py # E: 无 branch 磁铁推断
python scripts/result_axis_phase2.py       # I: bug-prone/cycle-time/churn
python scripts/task_type_playbooks.py      # F: 过程 playbook/failure
# D 可选（粗）: python scripts/bug_iteration_links.py
```

**2. 跑 story（全自动）**——注入自动触发（**前提：story 有 task_type**）：
```bash
story create <KEY> --autostart   # 或经 harness: run_real_story(...)
# design→build→verify，每个 stage 的 prompt 自动带 {knowledge_context}（高风险文件/cycle-time/磁铁）
```

**3. 半自动**——web 点"复制提示词"（走 `release_prompt.py`，**注入待接** = follow-up）。

**4. 门禁**——verify 阶段 HIGH-severity findings 自动 block + 反思式 repair（profile 需开 `quality.enabled + block_on_open_high_findings`）。

**5. 回流**——`miner refresh`（`refresh.sh`）采新 session/bug → 重跑 1 的脚本 → 知识变厚 → 飞轮转下一圈。

## 当前状态

- ✅ **所有 build brief（A–J）done + 验收 + 提交**（`c9dff60 / e1ec29a / b8d6c65 / d0e53cf / 49f8cbc`）。
- ✅ **G-run 实证**：credit-limit design 跑通，AI 引用 `bug_weight=29`、对高风险文件逐项评估回归 → 飞轮注入**被 AI 用上**。
- 🟡 **follow-up（parked，没发散）**：
  - 新 story **自动 task_type**（否则全新 story 无 task_type → 注入空转）。
  - miner refresh **定时化**（知识库自动长）。
  - `release_prompt.py`（半自动 web）**接注入**。
  - 基础知识（bootstrap 包）**接注入**（基础+增量两层合一）。
  - `transcripts.db` **备份**。
- ⏸ **严格 A/B**（多 story benchmark，with/without 比 bug 率）= 飞轮是否真降 bug 的统计级答案，更大工程，未做。

## 关键产物速查

```
packages/story-miner/scripts/out/
  bug_story_graph.json        # bug↔story + severity/status/time
  story_task_types.json       # 150 story → 12 类
  story_commits.json          # 19 story → commits（branch）
  story_commits_inferred.json # 8 磁铁 → commits（推断）
  result_axis_phase2.json/md  # bug-prone 文件 / cycle-time / churn
  outcome_knowledge.md        # 按 task_type 聚合的 outcome 知识（headline）
D:/hc-all/.story/knowledge/
  manifest.yaml / product.yaml / graph/ / scenarios/ / indexes/   # 基础（bootstrap）
  playbooks/{type}.md / failures/{type}.md                         # 过程（F）
```

## 一句话回顾每个轴的价值

- **结果轴**（A/I）：哪些 feature/类型**易出 bug**（marketing 3.5/故事、credit-limit 86 总量）——治种子偏见的实锤。
- **过程轴**（F）：每类**怎么干**（高频文件/命令/坑）。
- **基础**（bootstrap）：项目**长啥样**（域/服务/表/MQ）——已有，待接。
- **注入**（G）：把上面按 task_type **送进 prompt**，AI 用上（G-run 已证）。
- **门禁**（H）：verify **强制落地**（HIGH block + repair）。
- **硬关联**（J）：今后 story/commit **天然可追**（branch 带 story_key）。
