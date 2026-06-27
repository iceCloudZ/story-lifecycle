# dev-flywheel 落地 adoption 指南

> Monorepo：`packages/story-lifecycle`（流程引擎）+ `packages/story-miner`（对话挖掘）。
> 本仓库分析产物已经跑通，现在说明**怎么在日常工作流里用起来**。
> 先读 `packages/story-miner/CONTEXT.md` 了解项目结构，再读 `packages/story-miner/docs/ROADMAP.md` 了解任务卡。

---

## 一、产出物清单

| 产物 | 路径 | 来自 | 用途 |
|---|---|---|---|
| 约束规则表 | `docs/constraint-rules.md` | T1 | code review / 上线前检查 |
| 技术债务清单 | `scripts/out/debt.md` | T2 | 定期清理 TODO/FIXME/deprecated |
| 自动复盘 | `scripts/out/retrospect*.md` | T3 | 会话/Story 结束后的复盘 |
| 任务上下文包 | `scripts/out/context-package-*.md` | T4 | 开新任务前注入历史上下文 |
| SFT 语料 | `data/distill/sft-*.jsonl` | T5 | 模型微调训练数据 |
| 工作量预估表 | `scripts/out/effort-estimate.md` | T6 | 任务排期/工时评估 |
| 失败避坑清单 | `docs/failure-checklist.md` | T7 | 提交/构建/发布前检查 |
| 集成设计 | `docs/INTEGRATION.md` | 额外 | 与 story-lifecycle 联动的下一步 |

---

## 二、日常工作流怎么接

### 2.1 开新任务前：找历史上下文 + 估工时

**找相关历史会话（在 venv 中）：**
```bash
cd D:/github/story-lifecycle
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash
# 或 .venv-monorepo-test/bin/activate         # Linux/macOS
python packages/story-miner/scripts/recommend.py "你的任务关键词"
# 列表模式
python packages/story-miner/scripts/recommend.py "免息 清分" --package
# 上下文包模式（<500 字，可注入 prompt）
```

**估工作量：**
```bash
python packages/story-miner/scripts/predict.py
# 看 packages/story-miner/scripts/out/effort-estimate.md
```

用法：
- 先按工作区 + 任务类型查主表，得到 turns/tools 的中位/P90。
- 如果已知 story 复杂度（S/M/L），再用细化表校正。
- P90 远高于中位时，说明该类任务波动大，建议按 P90 给排期上界或拆任务。

---

### 2.2 编码中 / Code Review：约束检查

**本地速查（在 hc-all 工作区运行）：**
```bash
# 1. 对客中文
rg -n '\p{Han}' --type java -g '!**/test/**'
rg -n '\p{Han}' frontends/hc-admin/src

# 2. 硬编码 ID / token / 手机号
rg -n 'experiment_id|activity_id|token|phone|mobile|\b\d{10,}\b' --type java --type ts -g '!**/test/**'

# 3. TODO/FIXME/占位符
rg -n 'TODO|FIXME|HACK|_TEST_|_PLACEHOLDER|_DUMMY|_TEMP|System\.out|printStackTrace|console\.log' -g '!**/test/**'

# 4. 外部数据 valueOf 无防御
rg -n '\.valueOf\(' --type java -g '!**/test/**'

# 5. 非源码/中间产物提交
rg -n '\.docx?|\.tmp|\.log|/tmp/|\.claude/tmp|\.story/.*\.json|\.pyc|node_modules' --type java --type ts -g '!**/test/**'
```

更系统的方式：调用 `code-standards-check` skill（规则来源见 `packages/story-miner/docs/constraint-rules.md`）。

---

### 2.3 提交 / 构建 / 发布前：失败模式拦截

按 `docs/failure-checklist.md` 逐项核对：

| 时机 | 检查项 |
|---|---|
| 每次 Edit/Write 前 | 是否先 Read 了目标文件 |
| 本地修改后 | `mvn clean install` / `tsc --noEmit` / `yarn build` |
| Bash 失败后 | 先看 exit code + stderr，别直接重试 |
| 提交前 | `git status` 无 `UU/AA/DD` 冲突标记 |
| 跑 Python 脚本前 | venv / PYTHONPATH / 依赖是否正确 |
| 危险操作前 | `git push -f` / `drop table` / 覆盖配置必须显式确认 |

建议宿主：
- `build-check`：第 1、2、3、5、6、7、9 项
- `pre-release-review`：第 8、10 项

---

### 2.4 定期债务清理

每周/每迭代跑一次：
```bash
python scripts/debt.py
```

看 `scripts/out/debt.md`：
- 只关注 `.java/.ts/.tsx/.sql` 真实源码文件
- 对 Top 文件用 codegraph 或 IDE 二次确认债务是否仍在
- 优先处理 P0：TODO 临时关闭、硬编码、deprecated 调用

---

### 2.5 复盘：会话级 / Story 级

**单会话复盘：**
```bash
python -m miner.story_ingest          # 先刷新 stories 表
python -m miner.link                  # 刷新 session↔story 绑定
python packages/story-miner/scripts/retrospect.py <sid>
# 输出 packages/story-miner/scripts/out/retrospect_<sid_safe>.md
```

**批量 Top5：**
```bash
python packages/story-miner/scripts/retrospect.py
# 输出 packages/story-miner/scripts/out/retrospect.md
```

**Story 级复盘：**
```bash
python packages/story-miner/scripts/retrospect.py --story <story_key>
# 输出 <ws>/.story/done/<story_key>/retrospect.md
```

也已接入 story-lifecycle `story done <key>`：标记完成时会自动调用上述命令生成合并复盘（使用当前 venv 的 `sys.executable`）。

---

### 2.6 模型微调：使用 SFT 语料

语料位置：
```
data/distill/sft-<timestamp>.jsonl
```

已复核 20 条抽样，确认无 PII 泄露后可启用。

**加载示例：**
```python
from datasets import load_dataset

ds = load_dataset(
    "json",
    data_files="D:/github/story-lifecycle/packages/story-miner/data/distill/sft-<timestamp>.jsonl"
)
```

**格式：** ShareGPT 风格
```json
{
  "id": "claude:...",
  "source": "claude",
  "workspace": "hc-all",
  "turns": 13,
  "tools": 98,
  "errors": 3,
  "conversations": [
    {"from": "human", "value": "..."},
    {"from": "gpt", "value": "..."}
  ]
}
```

如需 parquet，安装 `pandas` + `pyarrow` 后可扩展。

---

## 三、如何保持数据新鲜

已提供定时脚本，建议由 Hermes / cron / Windows 计划任务调用：

```bash
# 每日增量入库 + 重算 playbook
cd D:/github/story-lifecycle
source .venv-monorepo-test/Scripts/activate
packages/story-miner/scripts/refresh.sh

# 每周全量 + 重算 playbook/failure
cd D:/github/story-lifecycle
source .venv-monorepo-test/Scripts/activate
packages/story-miner/scripts/refresh.sh full
```

**按需重跑分析：**
```bash
python packages/story-miner/scripts/constraint.py
python packages/story-miner/scripts/debt.py
python packages/story-miner/scripts/predict.py
python packages/story-miner/scripts/failure_mode.py
```

> 增量细节和自动化方案见 `docs/INTEGRATION.md` I1。

---

## 四、关键注意事项

1. **金融 PII 红线**
   - `data/*.db`、`data/distill/`、`.story/runs/` 含真实对话，**绝不入 git**。
   - SFT 语料离开本地前必须完成人工复核。

2. **ntools/nerrs 列值不可靠**
   - 所有分析脚本已从 `events` 表重算，不要直接信 `sessions.ntools` / `sessions.nerrs`。

3. **story 关联率**
   - 全库：67/331 sessions 已绑定（20.2%）。
   - 有 `.story/` 目录的工作区（hc-all）：story-sign 会话绑定率 80.4%，已满足 I2 指标。
   - 其他工作区（story-lifecycle/java-agent/github）目前无 `.story/` story 记录，因此无绑定目标。
   - 提升方案见 `docs/INTEGRATION.md` I2（story-lifecycle 主动写锚点）。

4. **路径展示**
   - T3/T4 已使用 `generate_playbooks.short()`，保留服务前缀和 basename，不再丢首字母。

---

## 五、常见问题

**Q: 我可以只跑某个脚本吗？**  
A: 可以。每个脚本独立，只读 db，写 `scripts/out/` 或 `data/distill/`。

**Q: 分析结果跟实际代码不一致？**  
A: 产物是历史高频访问/债务标记，代码可能已变。实际改动前用 codegraph/IDE 二次确认。

**Q: 想新增一条约束检查？**  
A: 改 `packages/story-miner/scripts/constraint.py` 的 `EXECUTABLE_RULES`，再同步到对应工作区的 `.agents/skills/code-standards-check/SKILL.md`。

**Q: 想扩大 SFT 语料规模？**  
A: 调 `packages/story-miner/scripts/distill.py` 里的 `MIN_TURNS` / `MAX_TURNS` / `MIN_TOOLS` / `MAX_ERR_RATIO`，重新导出后再次人工复核。

---

## 六、I1–I4 已完成

- **I1 定时扫描**：`packages/story-miner/scripts/refresh.sh`（每日增量 / 每周全量）。
- **I2 story↔session 主动绑定**：`miner/link.py` 读 `story-lifecycle` 写的 `anchors.jsonl`，hc-all 工作区 story-sign 会话绑定率 80.4%。
- **I3 provider 注入**：`story-lifecycle` 默认启用 `miner.story_context_provider`，自动注入 `{transcript_context}`。
- **I4 done 复盘钩子**：`story done <key>` 自动调用 `retrospect.py --story <key>`。

验证文档：`packages/story-miner/scripts/out/i2-i4-verify.md`。

## 七、下一步可推进（ROADMAP）

- **T1 约束库产品化**：把约束规则挂到 skill，自动检查。
- **T4 任务上下文包**：把 `recommend.py --package` 输出对齐到 `{transcript_context}`，和 I3 自然衔接。
- **T7 失败模式避坑清单**：把高频失败转成 `pre-release-review` / `build-check` 检查项。
- **T2 债务雷达打磨**：去噪声，只扫真实源码文件。
- **T6 工作量预估**：按 ws × 任务类型 × 复杂度 出基线表。
- **T8 三端 benchmark**：等样本充足后做同题对比评分卡。
