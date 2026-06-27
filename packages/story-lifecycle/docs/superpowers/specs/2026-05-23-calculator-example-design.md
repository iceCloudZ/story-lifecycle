# 示例项目：表达式计算器

## 目标

为 story-lifecycle 开源项目创建一个开箱即用的示例项目（`examples/calculator/`），让用户能快速体验完整的 design → implement → test 三步流程。

示例项目包含真实的 PRD 和预置测试用例，AI 实现后运行 pytest 验证结果。不需要修改 story-lifecycle 核心代码。

## 目录结构

```
examples/calculator/
├── PRD.md                  # 产品需求文档（中文）
├── tests/
│   ├── __init__.py         # 空文件，使 tests 成为包
│   └── test_calculator.py  # 预置测试用例（红灯状态）
└── README.md               # 快速开始指南
```

## PRD.md — 需求文档

定义一个 `Calculator` 类，放在 `calculator.py` 中，功能：

- **四则运算**：`add(a, b)`, `subtract(a, b)`, `multiply(a, b)`, `divide(a, b)`
- **幂运算**：`power(base, exp)`
- **取模**：`mod(a, b)`
- **链式调用**：`Calculator().add(2).multiply(3).result() == 6`
  - 链式方法：`add(v)`, `subtract(v)`, `multiply(v)`, `divide(v)`, `result()`
  - `result()` 返回当前累计值
- **异常处理**：除零抛 `ZeroDivisionError`，取模除零同理
- **纯 Python**，无第三方依赖

## tests/test_calculator.py — 预置测试

覆盖所有 PRD 功能点和边界情况：

| 测试类别 | 用例 |
|---------|------|
| 基本四则运算 | `add(2,3)==5`, `subtract(5,3)==2`, `multiply(4,3)==12`, `divide(10,2)==5` |
| 除零异常 | `divide(1,0)` 抛 `ZeroDivisionError` |
| 幂运算 | `power(2,3)==8`, `power(2,-1)==0.5`, `power(2,0)==1` |
| 取模 | `mod(10,3)==1`, `mod(10,0)` 抛 `ZeroDivisionError` |
| 链式调用 | `Calculator().add(5).subtract(2).multiply(3).result()==9` |
| 链式除零 | `Calculator().add(5).divide(0)` 抛 `ZeroDivisionError` |

测试在 AI 实现前全部红灯。test 阶段 AI 运行 `pytest`，验证全绿。

## README.md — 使用指南

操作步骤：

1. `cd examples/calculator`
2. 启动服务：`story serve`
3. 通过 TUI 创建 story：
   - Story Key: `CALC-001`
   - Title: `实现表达式计算器`
   - PRD: `PRD.md`
   - Profile: `minimal`
4. 观察三阶段自动流转

说明示例项目的设计意图：预置测试代表验收标准，AI 在 implement 阶段实现代码，test 阶段运行测试验证。

## 不涉及的改动

- 不修改 story-lifecycle 核心代码
- 不添加新的 CLI 命令或 profile
- 不依赖外部服务或 API key（除 AI CLI 本身）
