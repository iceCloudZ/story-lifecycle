# 编排 LLM 能力提升思路（待设计）

## 现状

编排 LLM（orchestrator LLM）目前角色单一，主要充当"裁判"：

- **路由决策**：story 出错时判断 retry / skip / fail / wait_confirm
- **plan 阶段**：生成执行计划（需要 LLM 可用时）
- **review 阶段**：审查执行产出、生成 findings

编排便是一个调度器，缺少对项目环境的感知和主动分析能力。

## 提升方向

### 1. 测试基建检测（Test Infrastructure Detection）

**问题**：用户项目不一定有可运行、可信、快速的测试基建。当前系统假设 review 阶段能基于测试结果做判断，但如果项目根本没有测试框架，review 就缺少客观验证手段。

**方案**：编排 LLM 在 plan 阶段主动扫描用户仓库，检测测试基础设施完备度。

检测内容：

| 检测项 | 扫描目标 | 说明 |
|--------|----------|------|
| 测试框架 | `pytest.ini`、`jest.config.*`、`Makefile` 中的 test target | 判断项目使用什么测试框架 |
| 测试目录 | `tests/`、`test/`、`__tests__/`、`*_test.go` | 判断是否有测试代码 |
| CI 配置 | `.github/workflows/`、`Jenkinsfile`、`.gitlab-ci.yml` | 判断是否有自动化测试流水线 |
| 覆盖率配置 | `coverage.py`、`nyc`、`istanbul` | 判断是否有覆盖率度量 |
| Mock/Stub 基建 | `conftest.py`、`__mocks__/`、`setup fixtures` | 判断测试隔离能力 |

产出结构：

```json
{
  "test_readiness": {
    "level": "L0|L1|L2|L3|L4",
    "framework": "pytest",
    "has_tests": true,
    "has_ci": true,
    "has_coverage": false,
    "test_command": "pytest",
    "gaps": ["无覆盖率配置", "无集成测试"],
    "recommendation": "review 阶段应侧重语义审查，测试结果仅作辅助信号"
  }
}
```

与 [[idea-plan-review-adversarial-loop]] 中的验证梯子对应：

```text
L0 diff inspection          — 始终可用
L1 syntax / compile check   — 检测构建工具后可用
L2 lint / format check      — 检测 linter 配置后可用
L3 targeted smoke command   — 检测到测试框架后可用
L4 project test command     — 检测到完整测试基建后可用
```

### 2. Plan 阶段增强：测试就绪评估

**当前**：plan 阶段只生成执行计划（做什么、怎么做）。

**增强**：plan 产出应包含测试就绪评估，直接影响后续阶段行为。

具体影响：

- **implement 阶段提示词**：如果测试基建完备（L3+），提示 AI 编写/更新测试；如果不完备（L0-L1），不要求写测试，但建议添加基本验证
- **review 阶段策略**：根据 test_readiness level 调整审查严格度。测试不可用时，提高语义审查权重，降低对自动化验证的依赖
- **adversarial loop 中的验证梯子**：reviewer 根据可用验证层级选择检查手段

Plan 产出扩展：

```json
{
  "plan": "...",
  "test_readiness": { ... },
  "review_strategy": "heavy_semantic | balanced | test_driven",
  "risks": ["项目无测试框架，代码质量仅靠 review 保障"]
}
```

### 3. 项目结构感知

编排 LLM 不应只看当前 stage 的文件，还应该理解项目整体结构：

| 能力 | 用途 |
|------|------|
| 语言/框架识别 | 扫描 `package.json`、`pyproject.toml`、`go.mod`、`pom.xml` 等 |
| 依赖关系分析 | 识别关键依赖及其版本，辅助 risk 评估 |
| 目录结构理解 | 区分 source / test / config / docs，给 reviewer 提供更精准的 context |
| 变更影响范围 | 分析 diff 影响哪些模块，评估回归风险 |

这不需要每次都全量扫描，可以缓存结果，只在 story 首次进入时做一次。

### 4. 风险预警

编排 LLM 可以基于环境感知提供主动风险预警：

- **低测试覆盖率** + **大 diff** → 高回归风险，建议拆分或增加 review 轮次
- **无 CI 配置** → 代码质量无自动化保障，reviewer 应提高严格度
- **依赖版本过旧** → 可能存在兼容性问题
- **变更涉及核心模块**（如 auth、payment） → 建议启用双人 review

### 5. 编排 LLM 角色扩展总结

| 角色 | 当前 | 提升 |
|------|------|------|
| 裁判（Router） | ✅ 已有 | 不变 |
| 计划生成（Planner） | ✅ 已有 | 增加测试就绪评估和项目结构感知 |
| 审查员（Reviewer） | ✅ 已有 | 根据测试基建自适应审查策略 |
| 环境分析师 | ❌ 未有 | 新增，plan 阶段扫描项目环境 |
| 风险预警 | ❌ 未有 | 新增，基于环境分析给出风险提示 |
| 质量策略顾问 | ❌ 未有 | 新增，根据项目特征推荐验证策略 |

## 与现有架构的关系

不需要新增 graph 节点。这些能力自然落在现有节点内部：

```
execute_stage_node
  └─ 新增：环境扫描（首次进入 story 时）
      ├─ test_readiness 检测
      ├─ 项目结构感知
      └─ 风险评估

plan_stage_node
  └─ 增强：plan 产出包含 test_readiness + review_strategy

review_stage_node
  └─ 增强：根据 review_strategy 调整审查严格度
```

## 数据存储

- **test_readiness**：写入 `story.context_json`，一次检测，全程复用
- **项目结构缓存**：写入 `.story-knowledge/{story_key}/project-scan.json`
- **风险预警**：写入 `stage_log` 作为 event，同时注入 review prompt

## MVP 范围

**P0（最小可用）**：

1. plan 阶段增加 test_readiness 检测（扫描文件是否存在）
2. review 阶段根据 test_readiness 调整提示词（有测试 vs 无测试两套策略）
3. 检测结果写入 context_json

**P1（体验提升）**：

4. 项目语言/框架识别
5. 风险预警注入 review prompt
6. 项目结构缓存机制

**P2（高级）**：

7. 依赖分析
8. 变更影响范围评估
9. 自动推荐验证策略

## 状态

待设计，与 [[idea-plan-review-adversarial-loop]] 的验证梯子和 code loop 互补。
