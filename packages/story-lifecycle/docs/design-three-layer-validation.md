# 设计文档：多层验证体系

> v0.6.0 开工前完成，为后续所有版本提供质量基线。

## 背景

v0.5.8 已有 SWE-bench runner（prepare → solve → export → eval）、E2E Scenario DSL（5 个 YAML 场景）和基础 CLI 命令入口回归测试。但存在多个盲区：

1. **SWE-bench 太简单**：顶级模型 pass rate 已超 80%，无法有效区分编排策略好坏。SWE-bench Pro（1865 任务，多语言，top ~45%）是直接升级。
2. **只能测 bug fix**：Story Lifecycle 编排的是 design → implement → test 全流程，但 SWE-bench 只给 issue description 让 agent 修 bug。需要能测"从需求到完整项目"的 bench。
3. **无法评估上下文选择质量**：Orchestrator Agent、Context Sharding、Task Packet 的核心风险不是最终 patch 是否过，而是 agent 是否拿到了正确上下文、是否浪费 token、是否遗漏关键文件。
4. **内部状态不可观测**：DecisionEnvelope、Policy Engine、Working Memory、Budget Ledger 等内部逻辑，外部 benchmark 的 pass rate 无法反映。需要 E2E scenario 覆盖。
5. **维护性和回归风险不足**：真实工程不仅要“做出来”，还要不破坏已有功能、CI 稳定、结构可维护、迁移安全。

因此验证体系不应只有三层，而应拆成五类验证目标：

```text
Layer 1: Issue / Patch Bench
  → 测 issue resolution 和 patch 产出能力
  → SWE-bench Pro / SWE-bench Multilingual / SWE-bench Multimodal

Layer 2: Feature / Project Bench
  → 测复杂功能开发和完整项目生成能力
  → FeatureBench / ProjDevBench

Layer 3: Context / Process Bench
  → 测上下文检索、上下文切片和 Task Packet 质量
  → ContextBench / internal context-sharding metrics

Layer 4: Orchestrator Protocol E2E
  → 测内部逻辑（Policy Engine、Budget、Working Memory、Blackboard）
  → 每个 roadmap 功能写 scenario 覆盖

Layer 5: Maintainability / Regression Bench
  → 测 CI 稳定性、回归风险、迁移和长期维护能力
  → SWE-CI / RepoMod-Bench / internal regression suite
```

---

## Layer 1: Issue / Patch Bench

Layer 1 的目标是验证 issue resolution、patch extraction、router/reviewer/gate 对最终修复率的影响。它不验证完整产品需求拆解，也不验证企业生产约束。

候选 benchmark：

- SWE-bench Verified：当前 baseline，适合回归。
- SWE-bench Pro：更难、多语言、更长上下文，适合区分高阶编排策略。
- SWE-bench Multilingual / Multimodal：用于覆盖非 Python 语言和包含 UI/截图线索的 issue。

短期优先接 SWE-bench Pro，因为当前 runner 已经是 SWE-bench 形态，改造成本最低。接完 Pro 后可快速接入 Multilingual（同一格式，只是语言更多）。

### SWE-bench Multilingual（Pro 后的 quick win）

- **官网**: [swebench.com](https://www.swebench.com/)
- **覆盖**: 9 种编程语言（C、C++、Go、Java、Rust 等）
- **格式**: 与 Verified / Pro 相同，同一个 eval harness
- **集成成本**: 接完 Pro 后几乎零成本，只是 `--benchmark multilingual` 切换数据源
- **价值**: 验证 adapter 多语言支持（Go/Java 等），为 FeatureBench 做铺垫

### SWE-bench Pro 接入

### 现状

- `benchmarks/swebench.py` 有完整的 prepare → solve → export → eval 流程
- `SWEbenchInstance` 数据类、`RunStore` run 管理、`BudgetConfig` 预算配置
- `load_instances_jsonl()` 从 JSONL 加载实例
- `checkout_instance()` 从 GitHub/Gitee mirror clone + checkout
- `profiles/swebench.yaml` 四阶段 profile

### SWE-bench Pro 差异

| 维度 | SWE-bench Verified | SWE-bench Pro |
|------|-------------------|---------------|
| 任务数 | 500 | 1,865 |
| 语言 | Python only | Python, Go, ... |
| 难度 | top ~93% | top ~45% |
| 仓库 | 12 | 41 (活跃维护) |
| 格式 | JSONL | public split 可能为 CSV/Parquet/JSONL，字段需 adapter 归一化 |

### 改动范围

不能假设 Pro 只是换 URL。公开数据 split、文件格式和字段可能与 Verified 不完全一致，因此需要 dataset adapter：

- 支持 JSONL / CSV / Parquet 输入。
- 支持 public split / held-out split 区分。
- 将外部字段归一化到 `SWEbenchInstance`。
- 保留 unknown fields 到 manifest，方便后续分析。
- 不假设完整 1,865 任务都可直接下载或本地评估。

在 adapter 归一化完成后，后续 prepare / solve / export / eval 尽量复用现有 runner。

#### 1. CLI 新增 `--benchmark` 参数

```python
# cli/swebench.py

@prepare.command()
@click.option("--benchmark", default="verified", type=click.Choice(["verified", "pro"]))
@click.option("--instances", default=None, help="Path to instances JSONL")
def prepare(benchmark, instances, ...):
    if instances:
        path = Path(instances)
    else:
        path = _download_benchmark(benchmark)
    ...
```

#### 2. 新增 benchmark 数据下载

```python
# benchmarks/registry.py

_BENCHMARK_URLS = {
    "verified": "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/resolve/main/swe-bench-verified.jsonl",
    "pro": "https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro",
}

def resolve_benchmark(name: str, cache_dir: Path | None = None) -> Path:
    """下载或缓存 benchmark 数据文件，返回本地路径。"""
    ...
```

#### 2.1 新增 dataset adapter

```python
# benchmarks/datasets.py

class BenchmarkDatasetAdapter(Protocol):
    def load(self, path: Path) -> list[dict]: ...
    def normalize(self, raw: dict) -> SWEbenchInstance: ...

class SWEbenchVerifiedAdapter:
    ...

class SWEbenchProAdapter:
    ...
```

#### 3. BudgetConfig 新增 pro 预设

```python
_BUDGET_PRESETS = {
    "smoke": {...},
    "standard": {...},
    "leaderboard": {...},
    # Pro 任务更难，默认需要更多预算
    "pro_smoke": {"max_rounds": 2, "max_review_rounds": 1, ...},
    "pro_standard": {"max_rounds": 4, "max_review_rounds": 3, ...},
}
```

#### 4. prepare_instance 适配多语言

当前 `_render_prd()` 只生成 Python 风格 PRD。Pro 有 Go 等语言，需要检测 repo 语言并调整 PRD 模板提示（如 test command、package manager）。

```python
def _detect_language(repo: str) -> str:
    """从 repo 名推断主要语言（粗粒度）。"""
    go_repos = {"golang/go", "kubernetes/kubernetes", "prometheus/prometheus", ...}
    if repo in go_repos:
        return "go"
    return "python"

def _render_prd(inst: SWEbenchInstance) -> str:
    lang = _detect_language(inst.repo)
    # 语言特定提示注入到 PRD
    ...
```

#### 5. export 预测格式不变

SWE-bench Pro 使用相同的 predictions.jsonl 格式（instance_id + model_patch），无需改动 `export_predictions()`。

#### 6. eval harness 适配

SWE-bench Pro 的 eval harness 支持多语言。CLI `eval` 命令需要接受 harness 参数：

```python
@eval.command()
@click.option("--harness", default="swebench", type=click.Choice(["swebench", "swebench-pro"]))
def eval(harness, ...):
    ...
```

### 文件改动清单

| 文件 | 改动 |
|------|------|
| `benchmarks/registry.py` | **新增**。benchmark 数据源管理、下载缓存 |
| `benchmarks/datasets.py` | **新增**。Verified / Pro dataset adapter，归一化不同格式 |
| `benchmarks/swebench.py` | `_BUDGET_PRESETS` 增加 pro 预设；`_render_prd` 多语言适配 |
| `cli/swebench.py` | prepare/solve/eval 新增 `--benchmark` 参数 |
| `profiles/swebench.yaml` | 可选增加 `swebench-pro` profile（更多 review 轮次） |

### 验收标准

- [ ] `story swebench prepare --benchmark pro --budget pro_smoke --limit 5` 能跑通
- [ ] pass rate 低于 verified baseline（说明任务确实更难）
- [ ] Go 语言实例不 crash
- [ ] Pro dataset adapter 能处理 public split 的实际文件格式
- [ ] manifest 保留 benchmark/source/split/version 信息

---

## Layer 2: Feature / Project Bench

Layer 2 的目标是验证 Story Lifecycle 最核心的能力：从需求到设计、实现、测试、交付，而不是只修一个 issue。

候选 benchmark：

- FeatureBench：复杂 feature-oriented development，更贴近“在已有项目中新增功能”。
- ProjDevBench：从需求生成完整项目，更贴近“从零或骨架项目完成工程交付”。

短期建议优先 spike FeatureBench，再接 ProjDevBench。原因是 Story Lifecycle 更常见的真实场景是“在既有业务项目里新增功能”，而不是从零生成完整项目。

### FeatureBench 适配

- **论文**: [arxiv.org/abs/2602.10975](https://arxiv.org/abs/2602.10975)（ICLR 2026 accepted）
- **GitHub**: [github.com/LiberCoders/FeatureBench](https://github.com/LiberCoders/FeatureBench)
- **官网**: [libercoders.github.io/FeatureBench](https://libercoders.github.io/FeatureBench/)
- **当前 agent 成功率**: ~11%（说明任务确实难，区分度远高于 SWE-bench 的 80%+）
- **评估方式**: test-driven，execution-based，automated task generation（可扩展、抗数据泄漏）

FeatureBench 测的不是”修一个 bug”，而是”在已有项目中新增一个完整功能”。它比 SWE-bench 更能暴露”会修 bug 但不会做 feature”的差距。

需要验证的能力：

- 需求理解（feature brief → implementation plan）。
- 影响范围分析（哪些文件需要改、哪些不能动）。
- 多文件修改（跨模块协调）。
- 新增功能与既有代码兼容（不破坏现有行为）。
- 测试补充（为新功能写测试）。
- review / retry 后质量提升。

**集成前提**: 接完 SWE-bench Pro + Multilingual 后，多语言 PRD 渲染已就绪，FeatureBench 可复用。

适配思路：

```text
Feature task
-> PRD / task brief
-> checkout repo
-> Story Lifecycle profile: design -> implement -> test -> review -> finalize
-> run benchmark tests
-> collect pass/fail + artifact quality + process trace
```

文件改动：

| 文件 | 改动 |
|------|------|
| `benchmarks/featurebench.py` | **新增**。FeatureTask 数据类、prepare/eval |
| `benchmarks/registry.py` | 新增 featurebench 数据源 |
| `profiles/featurebench.yaml` | **新增**。功能开发 profile |
| `cli/swebench.py` 或 `cli/bench.py` | 支持 `--benchmark featurebench` |

验收标准：

- [ ] `story bench prepare --benchmark featurebench --limit 3` 能跑通
- [ ] solve 走完整 design -> implement -> test -> review/finalize
- [ ] eval 输出 pass/fail、测试详情和 process trace
- [ ] summarize 能按 feature task 聚合结果

### ProjDevBench 适配

### 背景

ProjDevBench（[arxiv 2602.01655](https://arxiv.org/abs/2602.01655)，[GitHub](https://github.com/zsworld6/projdevbench)）给 agent 项目需求，评估生成的完整仓库。这和 SWE-bench 的"给 bug description，修 bug"完全不同——更接近 Story Lifecycle 的真实使用场景（design → implement → test）。

需要注意：ProjDevBench 更偏“从需求生成完整项目 / OJ 式测试”的能力验证，不能完全代表企业业务项目改造。它适合补充验证项目结构、构建、测试和完整交付，但不应替代 FeatureBench 或内部业务回归。

ProjDevBench 主要验证：

- requirements -> design -> implement -> test。
- repo structure。
- build/test loop。
- 多文件项目生成。

不适合单独验证：

- 企业业务系统改造。
- 数据库迁移安全。
- REST API 兼容性。
- 真实前端交互。
- 长期维护性。

### ProjDevBench 数据模型

```json
{
  "task_id": "proj-001",
  "requirements": "Build a REST API for a todo app with user authentication...",
  "language": "python",
  "test_command": "pytest tests/",
  "evaluation": {
    "type": "test_suite",
    "test_files": ["tests/test_api.py", "tests/test_auth.py"]
  },
  "constraints": {
    "max_files": 15,
    "required_packages": ["fastapi", "sqlalchemy"]
  }
}
```

### 架构设计

ProjDevBench 不是"修一个文件"，而是"生成一个项目"。评估维度不同：

| 维度 | SWE-bench | ProjDevBench |
|------|-----------|--------------|
| 输入 | issue description | 项目需求文档 |
| 输出 | git diff patch | 完整仓库 |
| 评估 | test pass rate | test pass rate + 代码质量 + 架构合理性 |
| 执行 | 在已有 repo 上改 | 从零或从骨架开始 |

核心思路：复用 `benchmarks/swebench.py` 的 RunStore / BudgetConfig 管理，但替换 instance 加载和 evaluation pipeline。

#### 1. 新增 ProjDevBench 数据类

```python
# benchmarks/projdev.py

@dataclass
class ProjDevTask:
    task_id: str
    requirements: str
    language: str
    test_command: str
    evaluation_type: str  # "test_suite" | "manual_review" | "linter"
    constraints: dict
    workspace_template: str | None = None  # 可选：骨架代码 zip/tar
```

#### 2. Instance → Story 映射

SWE-bench 映射到 `swebench` profile（design → implement → test → finalize）。ProjDevBench 映射到不同的 profile：

```yaml
# profiles/projdev.yaml
stages:
  design:
    cli: claude
    model: sonnet
    expected_outputs:
      - architecture_doc
      - api_spec
      - data_model
  implement:
    cli: claude
    model: sonnet
    expected_outputs:
      - files_created
      - implementation_summary
  test:
    cli: claude
    model: sonnet
    expected_outputs:
      - test_command
      - test_result
      - coverage_report
  finalize:
    cli: claude
    model: default
    expected_outputs:
      - project_structure
      - dependency_list
```

关键差异：ProjDevBench 的 workspace 不是 checkout 已有 repo，而是从空目录或模板开始。

```python
def prepare_projdev_task(
    task: ProjDevTask,
    workspace: Path,
    run_id: str,
) -> dict:
    """将 ProjDevBench task 映射为 Story。"""
    workspace = workspace.resolve()

    # 1. 如果有 workspace_template，解压到 workspace
    if task.workspace_template:
        _extract_template(task.workspace_template, workspace)

    # 2. 写需求文档（相当于 PRD）
    prd_path = workspace / "prd" / f"{task.task_id}.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(_render_requirements_md(task), encoding="utf-8")

    # 3. 写测试文件（如果 evaluation_type == test_suite）
    if task.evaluation_type == "test_suite":
        test_dir = workspace / "tests"
        test_dir.mkdir(exist_ok=True)
        # ProjDevBench 自带测试用例，拷贝到 workspace
        _write_test_suite(task, test_dir)

    # 4. 创建 Story
    story_key = f"{run_id}__{task.task_id}"
    context = {
        "benchmark": "projdev",
        "run_id": run_id,
        "task_id": task.task_id,
        "language": task.language,
        "test_command": task.test_command,
        "prd_path": str(prd_path),
        "constraints": task.constraints,
    }
    db.upsert_story(story_key=story_key, ...)
    db.update_story(story_key, context_json=json.dumps(context))
    ...
```

#### 3. Evaluation Pipeline

SWE-bench 用官方 harness（Docker 内跑 test）。ProjDevBench 评估需要：

```text
1. test_suite: 在 workspace 跑 task.test_command，收集 pass/fail
2. linter: 跑 lint 工具（ruff/pylint for Python, golangci-lint for Go）
3. structure_check: 检查项目结构合理性（有 setup.py/pyproject.toml、src/ 目录等）
4. completeness: 检查 requirements 中的每条需求是否有对应实现
```

```python
# benchmarks/projdev_eval.py

@dataclass
class ProjDevEvalResult:
    task_id: str
    test_pass_rate: float
    test_total: int
    test_passed: int
    lint_score: float  # 0-1
    structure_score: float  # 0-1
    completeness_score: float  # 0-1，需求覆盖率
    overall_score: float  # 加权平均

def evaluate_projdev(
    workspace: Path,
    task: ProjDevTask,
) -> ProjDevEvalResult:
    """评估 ProjDevBench task 产出。"""
    results = {}

    # 1. 跑测试
    if task.evaluation_type == "test_suite":
        results["test"] = _run_test_suite(workspace, task.test_command)

    # 2. 跑 lint
    results["lint"] = _run_linter(workspace, task.language)

    # 3. 检查结构
    results["structure"] = _check_structure(workspace, task.language)

    # 4. 需求覆盖率（LLM 辅助：给定需求和代码，判断每条需求是否实现）
    results["completeness"] = _check_completeness(workspace, task.requirements)

    # 5. 加权综合
    return _aggregate_scores(results)
```

#### 4. CLI 集成

复用 `story swebench` 命令结构，新增 `--benchmark projdev` 参数：

```bash
# 加载 ProjDevBench tasks 并 prepare
story swebench prepare --benchmark projdev --limit 3

# 跑 solve（和 swebench 共用 RunStore + graph 执行）
story swebench solve --run-id <run_id>

# 评估（走 ProjDevBench 自己的 evaluation pipeline）
story swebench eval --run-id <run_id> --benchmark projdev
```

#### 5. summarize 扩展

```python
def summarize_run(store: RunStore, run_id: str) -> dict:
    manifest = store.load_manifest(run_id)
    benchmark = manifest.get("benchmark", "swebench")

    if benchmark == "projdev":
        return _summarize_projdev(manifest)
    else:
        return _summarize_swebench(manifest)
```

### 与 roadmap 功能的验证关系

ProjDevBench 比 SWE-bench 更能验证以下功能：

| Roadmap 功能 | SWE-bench 能测？ | ProjDevBench 能测？ |
|-------------|-----------------|-------------------|
| Complexity Classifier | 部分（实例偏小） | **是**（项目级复杂度差异大） |
| 阶段交接包 | 部分（间接） | **是**（design→implement 交接质量直接影响产出） |
| Working Memory | 部分 | **是**（项目级任务跨 stage 依赖更强） |
| Meta-Planner Decomposition | 部分 | **是**（大需求自然需要拆分） |
| Stage Graph 动态插入 | 部分 | **是**（项目级任务可能需要插入 research/security 阶段） |

### 文件改动清单

| 文件 | 改动 |
|------|------|
| `benchmarks/projdev.py` | **新增**。ProjDevTask 数据类、prepare_projdev_task、workspace 管理 |
| `benchmarks/projdev_eval.py` | **新增**。评估 pipeline（test/lint/structure/completeness） |
| `benchmarks/registry.py` | 新增 projdev 数据源 URL |
| `profiles/projdev.yaml` | **新增**。ProjDevBench 四阶段 profile |
| `cli/swebench.py` | prepare/eval/summarize 新增 `--benchmark projdev` |
| `benchmarks/swebench.py` | RunStore 支持 benchmark type 字段 |

### 验收标准

- [ ] `story swebench prepare --benchmark projdev --limit 3` 能跑通
- [ ] solve 走完整 design → implement → test → finalize 流程
- [ ] eval 输出 ProjDevEvalResult（test_pass_rate + lint_score + structure_score + completeness_score）
- [ ] summarize 输出 ProjDevBench 格式汇总

---

## Layer 3: Context / Process Bench

Layer 3 的目标是验证上下文选择和过程质量。对于 Orchestrator Agent、Plan-stage Decomposition、Context Sharding 来说，最终 pass/fail 不够。系统还需要知道：

- Planner 是否找到了正确文件。
- Task Packet 是否包含足够上下文。
- 子 Agent 是否遗漏关键上下文。
- 是否把太多无关上下文塞给执行 Agent。
- 上下文 token 成本是否下降。

### ContextBench 适配

- **论文**: [arxiv.org/abs/2602.05892](https://arxiv.org/abs/2602.05892)
- **GitHub**: [github.com/EuniAI/ContextBench](https://github.com/EuniAI/ContextBench)
- **数据**: 1,136 个实例，8 种编程语言，**人工标注 gold context**（文件级 + 符号级）
- **核心创新**: 在 end-to-end benchmark 基础上增加**中间过程指标**，"unbox" issue-resolution 过程
- **关键发现**: 当前高级 agent 设计在上下文检索质量上相比简单 baseline 改善有限

ContextBench 提供的 gold context 可以直接测量编排器的上下文选择质量——不仅看"最终 patch 是否过"，还看"agent 是否拿到了正确上下文"。

建议指标（与 ContextBench 对齐）：

```text
context_recall          # gold context 中被检索到的比例
context_precision       # 检索到的上下文中属于 gold 的比例
gold_file_hit_rate      # gold files 被命中的比例
used_context_ratio      # 检索到的上下文实际被使用的比例
context_token_cost      # 总上下文 token 数
retrieved_but_unused    # 检索但未使用的 token 数
task_packet_context_recall  # Task Packet 中 gold context 的覆盖度
```

与 Story Lifecycle 的关系：

| 功能 | ContextBench 可验证点 |
|------|----------------------|
| Context Sharding | 子任务包是否保留 gold context |
| Task Packet | packet 是否包含必要文件/符号/约束 |
| Working Memory | 是否把关键事实传递到后续 stage |
| Meta-Planner Decomposition | 拆分后每个 task 是否拿到正确上下文 |
| Resource Lock | 是否能根据上下文识别修改资源 |

适配思路：

```text
ContextBench task
-> run Story Lifecycle context discovery / planning
-> record retrieved files/snippets/symbols
-> compare with gold context
-> optionally continue solve and correlate context metrics with pass/fail
```

文件改动：

| 文件 | 改动 |
|------|------|
| `benchmarks/contextbench.py` | **新增**。ContextTask、gold context loader、metrics |
| `benchmarks/context_eval.py` | **新增**。recall/precision/token cost 计算 |
| `profiles/contextbench.yaml` | **新增**。只跑 discovery/plan 的轻量 profile |

验收标准：

- [ ] 能加载至少 10 个 ContextBench task
- [ ] 输出 context_recall / context_precision / token_cost
- [ ] 能将 Task Packet 的 relevant files 与 gold context 对比
- [ ] 能关联 context metrics 与最终 solve outcome

### Internal Context Metrics

即使不接外部 ContextBench，也应在内部 trace 记录：

```json
{
  "story_key": "S-001",
  "stage": "plan",
  "context_metrics": {
    "input_tokens": 12000,
    "packet_tokens": 1800,
    "relevant_files_count": 6,
    "referenced_files_count": 4,
    "unused_context_tokens": 3200
  }
}
```

这些指标用于判断 context sharding 是否真的节省 token，而不是把成本转移到更多 agent 调用。

---

## Layer 4: Orchestrator Protocol E2E

### 现状

`tests/e2e/` 已有基础框架：

- `scenario.py` — `Scenario` 类，加载 YAML 场景
- `fake_tool.py` — `FakeStageTool`，写入 `.story-done` 文件
- `runner.py` — `run_scenario()` + `assert_scenario_expect()`
- 5 个 YAML 场景：happy_path、review_retry_then_pass、missing_expected_output、sub_story_wait_resume、markdown_done_json

### 当前 Scenario DSL 能力

```yaml
story_key: E2E-HAPPY
title: Headless happy path
profile: minimal

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S

expect:
  status: completed
  final_stage: review
  context:
    spec_path: docs/spec.md
  event_counts:
    execute: 3
```

当前 DSL 只能定义 done payload 和 expect 最终状态。不能测：
- Policy Engine 的 allow/reject/needs_confirm 裁决
- Budget Ledger 的累计和超限
- Working Memory 的跨 stage 更新
- DecisionEnvelope 的 confidence 和 reason
- Strategic Router 的 shadow proposal
- Complexity Classifier 的分流
- Graph Patch 的 insert_stage / skip_stage

### DSL 扩展设计

#### 1. Policy 断言

```yaml
# tests/e2e/scenarios/policy_reject_destructive.yaml
story_key: E2E-POLICY-REJECT
title: Policy rejects destructive action
profile: minimal

policy:
  autonomy_level: L1  # 所有 apply 类动作需要 confirm

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
  implement:
    done:
      files_changed: [src/main.py]
    # 模拟 implement 后 router 提出 destructive decision
    router_decision:
      decision: delete_branch
      confidence: 0.9
      requires_human: true

expect:
  status: wait_confirm  # Policy 拦截，进入等待确认
  policy_decisions:
    - decision_id: dec-*
      result: needs_confirm
      blocked_actions: [delete_branch]
  event_counts:
    execute: 2  # design + implement
```

#### 2. Budget 断言

```yaml
# tests/e2e/scenarios/budget_exhaust.yaml
story_key: E2E-BUDGET
title: Budget exhaust triggers hard kill
profile: minimal

budget:
  max_llm_calls: 3
  max_retries: 1

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
  implement:
    executions:
      - done:
          files_changed: [src/main.py]
          implementation_summary: failed
        review_result: revise
      - done:
          files_changed: [src/main.py]
          implementation_summary: failed again
        review_result: revise

expect:
  status: failed  # 预算耗尽
  budget_exhausted: true
  budget_used:
    llm_calls: 3
    retries: 1
```

#### 3. Working Memory 断言

```yaml
# tests/e2e/scenarios/working_memory_update.yaml
story_key: E2E-WM
title: Working memory persists across stages
profile: minimal

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
      risks: ["migration needed"]
  implement:
    done:
      files_changed: [src/main.py]

expect:
  status: completed
  working_memory:
    confirmed_facts_contains: ["migration needed"]
    open_risks_not_empty: true
```

#### 4. Complexity Classifier 断言

```yaml
# tests/e2e/scenarios/simple_path_trivial.yaml
story_key: E2E-TRIVIAL
title: Trivial task takes simple execution path
profile: minimal

complexity_override: trivial  # 强制设置复杂度

stages:
  implement:
    done:
      files_changed: [README.md]

expect:
  status: completed
  execution_path: simple  # 验证走了简化路径
  skipped_stages: [design, review]  # trivial 跳过 design 和 review
```

#### 5. DecisionEnvelope 断言

```yaml
# tests/e2e/scenarios/decision_envelope.yaml
story_key: E2E-ENVELOPE
title: Router outputs structured DecisionEnvelope
profile: minimal

stages:
  design:
    done:
      spec_path: docs/spec.md
      complexity: S
  implement:
    done:
      files_changed: [src/main.py]
    router_output:
      decision: advance
      confidence: 0.92
      reason: "implementation matches spec"

expect:
  status: completed
  decisions:
    - stage: implement
      decision: advance
      confidence_gte: 0.8
      has_reason: true
```

### Runner 扩展

当前 `run_scenario()` 通过 mock planner + FakeStageTool 驱动 graph。需要扩展为：

```python
# tests/e2e/runner.py

def run_scenario(scenario: Scenario, tmp_path: Path) -> dict:
    """运行 E2E scenario，返回完整结果（含 policy/budget/memory 断言数据）。"""
    ...

    # 1. 如果场景定义了 policy 配置，设置 Policy Engine
    if scenario.policy:
        _configure_policy(scenario.policy)

    # 2. 如果场景定义了 budget，设置 Budget Ledger
    if scenario.budget:
        _configure_budget(scenario.budget, story_key)

    # 3. 如果场景定义了 complexity_override，跳过 classifier 直接设置
    if scenario.complexity_override:
        _override_complexity(story_key, scenario.complexity_override)

    # 4. 运行 graph（和现有逻辑相同）
    _run_story_impl(key)

    # 5. 收集结果
    result = {
        "story": db.get_story(key),
        "events": db.list_events(key),
        "working_memory": _load_working_memory(key),
        "budget": _load_budget_ledger(key),
        "policy_decisions": _load_policy_decisions(key),
        "decisions": _load_decision_envelopes(key),
    }
    return result


def assert_scenario_expect(result: dict, expect: dict):
    """扩展断言，支持 policy/budget/memory/decision 检查。"""
    ...

    # 原有断言：status, final_stage, context, event_counts
    ...

    # 新增：policy_decisions
    if "policy_decisions" in expect:
        _assert_policy_decisions(result["policy_decisions"], expect["policy_decisions"])

    # 新增：budget
    if "budget_exhausted" in expect:
        assert result["budget"]["exhausted"] == expect["budget_exhausted"]
    if "budget_used" in expect:
        for key, value in expect["budget_used"].items():
            assert result["budget"]["used"].get(key) == value

    # 新增：working_memory
    if "working_memory" in expect:
        _assert_working_memory(result["working_memory"], expect["working_memory"])

    # 新增：decisions
    if "decisions" in expect:
        _assert_decisions(result["decisions"], expect["decisions"])

    # 新增：execution_path
    if "execution_path" in expect:
        _assert_execution_path(result, expect)
```

### Scenario 与 Roadmap 的覆盖矩阵

每个 roadmap 功能至少一个 happy path + 一个边界场景：

| Roadmap 功能 | Happy Path 场景 | 边界场景 |
|-------------|----------------|---------|
| DecisionEnvelope | `decision_envelope.yaml` | `decision_low_confidence.yaml` |
| Policy Engine | `policy_reject_destructive.yaml` | `policy_shadow_only.yaml` |
| Complexity Classifier | `simple_path_trivial.yaml` | `trivial_circuit_break.yaml` |
| Simple Execution Path | `simple_path_trivial.yaml` | `simple_path_escalation.yaml` |
| Working Memory | `working_memory_update.yaml` | `memory_empty_first_stage.yaml` |
| Budget Ledger | `budget_happy.yaml` | `budget_exhaust.yaml` |
| Resource Lock | `resource_lock_no_conflict.yaml` | `resource_lock_conflict.yaml` |
| Strategic Router | `router_shadow_mode.yaml` | `router_shadow_mismatch.yaml` |
| Runtime Blackboard | `blackboard_provider_health.yaml` | `blackboard_stale.yaml` |
| Meta-Planner | `meta_planner_simple.yaml` | `meta_planner_decompose.yaml` |
| Decomposition | `decompose_happy.yaml` | `decompose_dependency_cycle.yaml` |
| Stage Graph | `graph_patch_insert.yaml` | `graph_patch_reject.yaml` |
| Graph Patch | `graph_patch_insert.yaml` | `graph_patch_budget_exceed.yaml` |
| Guarded Apply | `guarded_l3_auto.yaml` | `guarded_l1_needs_confirm.yaml` |
| Human Interrupt | `human_interrupt_structured.yaml` | `human_interrupt_budget.yaml` |

### 文件改动清单

| 文件 | 改动 |
|------|------|
| `tests/e2e/scenario.py` | 扩展 Scenario 类，支持 policy/budget/memory/decisions 字段 |
| `tests/e2e/runner.py` | 扩展 run_scenario / assert_scenario_expect |
| `tests/e2e/fake_tool.py` | 支持 router_decision / router_output 模拟 |
| `tests/e2e/scenarios/*.yaml` | **新增** ~15-20 个场景文件 |
| `tests/e2e/conftest.py` | 新增 policy/budget/memory fixtures |

### 验收标准

- [ ] 现有 5 个场景全部通过（不破坏已有）
- [ ] 新增 policy_reject_destructive 场景通过
- [ ] 新增 budget_exhaust 场景通过
- [ ] 新增 working_memory_update 场景通过
- [ ] 新增 simple_path_trivial 场景通过
- [ ] 新增 decision_envelope 场景通过
- [ ] `pytest tests/e2e/ -v` 全绿

---

## 多层覆盖矩阵

多层验证体系与 roadmap 功能的覆盖关系：

```text
功能                          Layer 1    Layer 2    Layer 3    Layer 4    Layer 5
                              Patch      Feature    Context    Scenario   Regression
───────────────────────────────────────────────────────────────────────────────
v0.6.0
  DecisionEnvelope            ·          ·          ·          ✓✓         ·
  Policy Engine               ·          ·          ·          ✓✓         ·
  Complexity Classifier       ·          ✓✓         ✓          ✓          ·
  Simple Execution Path       ·          ✓          ✓          ✓✓         ·
  架构审查门禁                ·          ✓          ·          ✓          ·
  阶段交接包                  ·          ✓✓         ✓✓         ✓          ·

v0.7.0
  梯度归因 / 模式提取         ✓✓         ✓          ✓          ·          ·
  偏好数据集                  ✓✓         ✓          ·          ·          ·
  CLI 对抗审查                ✓          ✓          ·          ·          ✓
  Working Memory              ·          ✓          ✓          ✓✓         ·
  Budget Ledger               ·          ·          ·          ✓✓         ·

v0.8.0
  PRD 输入增强                ·          ✓          ·          ·          ·
  多模型对比                  ✓          ✓          ·          ·          ✓
  Resource Lock Dry-run       ·          ·          ✓          ✓✓         ·

v0.9.0
  项目智能管道                ·          ✓          ✓✓         ✓          ·
  双飞轮治理                  ·          ·          ·          ✓✓         ·
  Strategic Router Shadow     ✓          ✓          ·          ✓✓         ✓
  Runtime Blackboard          ·          ·          ·          ✓✓         ·
  Meta-Planner Decomposition  ·          ✓✓         ✓✓         ✓✓         ·

v1.0.0
  Stage Graph + Graph Patch   ·          ✓          ·          ✓✓         ✓
  Guarded Apply               ✓          ✓          ·          ✓✓         ✓
  CI/CD / 文档 / 稳定性       ·          ·          ·          ·          ✓✓

覆盖率                        ~45%       ~60%       ~50%       ~70%       ~40%
```

标记说明：`✓✓` = 主要验证手段，`✓` = 辅助验证，`·` = 不适用

说明：矩阵中的覆盖率是验证目标覆盖估算，不是代码覆盖率。Layer 3/4 的价值主要是发现外部 benchmark 无法暴露的上下文和编排协议问题。

---

## Layer 5: Maintainability / Regression Bench

Layer 5 关注长期维护和回归风险。外部 benchmark 往往只看一次任务是否通过，但真实工程还关心：

- 是否破坏已有功能。
- CI 是否稳定。
- 改动是否可维护。
- 是否引入迁移风险。
- 大规模重构后行为是否等价。

候选：

- SWE-CI（[arxiv 2603.03823](https://arxiv.org/html/2603.03823v1)，[GitHub](https://github.com/SKYLENAGE-AI/SWE-CI)，Alibaba）：100 个任务，每个 ~233 天演化历史，~71 次连续 commit。核心发现：75% 的 agent 随时间推移会破坏自己之前的修复。**但注意**：SWE-CI 的 CI-loop 模型（连续多次 commit + 交叉验证）与 Story Lifecycle 的单 story 模型不完全匹配。适合作为 v0.9+ 的探索项，用于测试"连续 story 是否引入回归"。
- RepoMod-Bench（[arxiv 2602.22518](https://arxiv.org/abs/2602.22518)）：21 个真实仓库，8 种语言。**核心场景是跨语言代码翻译**（如 Python→Go），不是通用重构。与 Story Lifecycle 的日常使用场景匹配度较低，建议作为 v1.0+ 的扩展探索。
- Internal regression suite：固定 SWE-bench/FeatureBench/Scenario 子集，作为每次 release 必跑。**短期优先做这个**。

### Internal Regression Suite

建议先做内部回归集：

```text
regression/
  swebench_verified_smoke.txt
  swebench_pro_smoke.txt
  featurebench_smoke.txt
  contextbench_smoke.txt
  scenarios_required.txt
```

每次关键改动至少运行：

- E2E Scenario DSL 必跑。
- SWE-bench smoke 必跑。
- Context metrics smoke 必跑。
- 关键 profile 的 doctor/validate 必跑。

### SWE-CI Spike

SWE-CI（100 个任务，连续 CI loop）可作为 v0.9+ 的探索项：

- 验证连续 story 执行是否引入回归。
- 验证 Working Memory 跨 story 传递质量。
- **前提**: 需要先改造为"多次 story 连续执行 + 交叉验证"模式，当前单 story 模型不匹配。

### RepoMod-Bench（远期）

RepoMod-Bench（跨语言翻译）匹配度较低。如果未来有跨语言迁移场景（如 Python→Rust 重写），可作为专项测试。短期不建议投入。

---

### 无法被多层覆盖的功能

以下功能需要人工验证或独立测试手段：

| 功能 | 验证方式 |
|------|---------|
| PRD 输入增强（TAPD HTML→markdown） | 集成测试（mock TAPD API） |
| TUI 展示（面板、新鲜度、对比） | 手动测试 / screenshot regression |
| CI/CD 流水线 | 实际 GitHub Actions 运行 |
| 文档质量 | 人工 review |
| 安全审查 | 独立审计 |
| 开放生态（adapter 模板） | 社区反馈 |

---

## 实施顺序

```text
Phase 0（1-2 天）: Layer 4 — Orchestrator Protocol E2E
  → 不依赖外部数据源，直接保护 Policy/Budget/Memory/Envelope
  → 先写 5 个基础场景（DecisionEnvelope + Policy + Budget + Memory + Complexity）
  → 每个 roadmap 功能至少 happy path + edge case

Phase 1（1-2 天）: Layer 1 — SWE-bench Pro adapter
  → 当前 runner 已有，改造成本低
  → 重点是 dataset adapter，不是只换 URL
  → 为后续版本提供 pass rate 基线
  → 顺手接入 Multilingual（同一格式，零额外成本）

Phase 2（2-3 天）: Layer 3 — ContextBench adapter
  → 直接验证 Context Sharding / Task Packet 质量
  → 先做 context metrics + gold context 对比，不必立即完整 solve
  → GitHub: github.com/EuniAI/ContextBench
  → 1,136 实例，8 语言，人工标注 gold context

Phase 3（3-5 天）: Layer 2 — FeatureBench spike
  → ICLR 2026，当前 agent 成功率 ~11%
  → 最贴近"在已有项目中新增功能"的真实场景
  → GitHub: github.com/LiberCoders/FeatureBench
  → 先跑通 prepare + solve + eval smoke

Phase 4（3-5 天）: Layer 2 — ProjDevBench adapter
  → 需要研究 ProjDevBench 数据格式和 eval pipeline
  → 先跑通 prepare + solve，再加 eval
  → 作为完整项目生成能力补充

Phase 5（持续）: Layer 5 — Maintainability / Regression
  → 固定 smoke set（verified + pro + featurebench + scenario 必跑）
  → 接入 CI
  → v0.9+ 探索 SWE-CI（CI loop 回归测试）
  → v1.0+ 按需探索 RepoMod-Bench（跨语言迁移场景）
```

Phase 0 和 Phase 1 可并行。Phase 2 建议早做，因为它直接验证 Orchestrator Agent 的 Context Sharding 风险，且 ContextBench 已有 1,136 个标注好的 gold context 实例。Phase 3/4 顺序可根据 FeatureBench/ProjDevBench 的 spike 结果调整。
