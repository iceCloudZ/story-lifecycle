# 测试架构（monorepo dev-flywheel）

> 业界验证的分层：**单元跟着包 + 跨项目集中 + 共享测试基础设施独立包 + 一个命令聚合**。
> 真实 E2E（real_e2e）是塔尖，默认跳过，手动/nightly 跑。

## 业界调研结论（2026-06-27）
- **单元跟着包（co-locate）**：Turborepo / Babel / React / Next.js / Python uv 强共识。理由：包自包含（可独立发布）、测试归属清晰、增量 CI（只跑受影响包）
- **E2E/integration 跨包集中**：Turborepo 原话 "E2E tests can be ... extracted to a dedicated testing package"
- **一个命令聚合（逻辑集中，非物理集中）**：Jest multi-project / pytest testpaths
- 来源：[Turborepo discussion](https://github.com/vercel/turborepo/discussions/2320)、[Babel config](https://babeljs.io/docs/config-files/)、[Buildkite CI](https://buildkite.com/resources/blog/monorepo-ci-best-practices/)、[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- **不把所有测试搬进一个包**：单元跟着包是共识，全集中有硬伤（离源码/失自包含/增量CI失效）

## 分层
| 层 | 位置 | 速度 | 验证什么 |
|---|---|---|---|
| 单元 | `packages/<pkg>/tests` | 快 | 子包内部逻辑 |
| 契约 | `tests/contracts` | 快 | 跨项目接口稳定（14 passed）|
| mock integration | `tests/integration`（FakeAdapter）| 快 | orchestrator 流程通 |
| **real e2e** | `tests/e2e`（`@real_e2e`）| 慢/贵 | **真实 AI 跑飞轮，产出正确代码** |

## packages/testing（dedicated testing package）
**共享测试基础设施**（被各测试 import），不是"装所有测试"：
- `harness.py`：`run_real_story`（调 story-lifecycle 真实 API `service.create_and_start_story` + `planner.continue_orchestrator_agent`，真实 AI 不 mock）
- `workspace.py`：calculator 重置（`git restore calculator.py` + 清 `.story/<key>`）
- `asserters.py`：每步产物断言（design/implement/verify/done/miner）
- `scenarios/calculator/`：真实 E2E workspace（PRD + 17 测试，红→绿）

## real_e2e 机制（A-E 决策，已定）
- **A 触发**：`@pytest.mark.real_e2e`，默认 `pytest` 跳过（`-m "not real_e2e"`）；`pytest -m real_e2e` 手动/nightly
- **B 重置**：每跑 `git restore calculator.py` + 清 `.story/<key>`（可重复）
- **C AI 依赖**：需 claude/codex CLI + key；不进常规 CI
- **D 强断言**：verify 要 17 测试全过（验证 AI 真写对代码）
- **E 失败诊断**：保留 transcript + 产物（失败 = 发现 AI/流程问题，是 feature）

## calculator scenario（真实 E2E 红→绿）
```
new → design(AI 读 PRD+test 写 spec) → implement(AI 写 calculator.py) 
    → verify(pytest 17 测试全过) → done(retrospect) + miner(store+link 绑定 story_id)
```
每步断言结构性产物（spec.md 非空 / calculator.py git diff 非空 / pytest exit 0 / retrospect.md 非空 / story_id=high）。

## 顶层聚合
`pyproject.toml` testpaths 串所有：`packages/*/tests` + `tests/{contracts,integration,e2e}` + `pythonpath`（三子包 + testing/src）。
- `pytest` → 单元 + 契约 + integration（默认，跳过 real_e2e）
- `pytest -m real_e2e` → 真实 E2E（手动/nightly）

## 不做什么
- 不把所有测试（含单元）搬进一个集中包（业界共识：单元跟着包）
- 不 mock real_e2e（真实 AI 是它的核心价值，mock 会退回 FakeAdapter 伪验证）
- real_e2e 不进常规 PR CI（慢/贵/需 AI key）
