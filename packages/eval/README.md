# eval — story-lifecycle 离线评测层（gold 数据集 + LLM judge + 回放回归）

> 本 README 完成版见 Task 4（首轮实测 token 数据会补进「成本与限流」节）。

## 是什么

离线 judge 层:不动管线 decider-pure 原则,只做**加法**:

- `eval extract` — 从生产 story.db（只读）+ 证据目录抽取 gold 数据集
- `eval score` — 对 core 集全量跑 LLM judge,生成历史基线
- `eval replay` — 用 gold PRD 驱动真实 opencode 回放 story（30min 熔断）
- `eval report` — 回放产出过 judge,输出回归报告（vs gold 基线 / 上次回放）

## 安装与 env 前置

```bash
pip install -e packages/eval
export OPENCODE_API_KEY=...          # Go 端点 key（judge/回放管线共用）
# 可选覆盖:EVAL_LLM_BASE_URL / EVAL_LLM_API_KEY / EVAL_LLM_MODEL（对比 judge 模型）
```

## 目录约定

```
dataset/    gold 抽取产物（gitignore）
results/    评分/回放/回归报告（gitignore）
replay_set.json  回放集清单
```

## 成本与限流

（Task 4 填充:首轮 baseline + replay 实测 token）

## 回归触发约定

修改 `infra/prompts/` 模板或 `orchestrator/evaluation/` 后应跑 `eval replay` 回归。
