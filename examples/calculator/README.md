# 表达式计算器 — Story Lifecycle 示例项目

一个开箱即用的示例，演示 story-lifecycle 的 design → implement → test 三步流程。

## 项目结构

```
calculator/
├── PRD.md                  # 产品需求文档（AI 在 design 阶段阅读）
├── tests/
│   └── test_calculator.py  # 预置测试（验收标准，当前红灯）
└── README.md               # 本文件
```

## 快速开始

### 前置条件

- 已安装 story-lifecycle：`pip install -e .`
- 已配置 AI CLI（如 Claude Code）
- 已运行 `story setup` 配置 LLM

### 运行示例

```bash
# 1. 进入示例目录
cd examples/calculator

# 2. 启动服务
story serve

# 3. 在另一个终端打开 TUI
story

# 4. 在 TUI 中创建 story：
#    - Story Key: CALC-001
#    - Title: 实现表达式计算器
#    - PRD: PRD.md
#    - Profile: minimal

# 5. 观察三阶段自动流转：
#    design（分析 PRD，写设计文档）
#    → implement（实现 Calculator 类）
#    → test（运行 pytest，验证全绿）
```

## 设计意图

- **PRD.md** 定义需求，AI 在 design 阶段分析后输出设计文档
- **tests/test_calculator.py** 是验收标准，当前全部红灯
- AI 在 implement 阶段实现 `calculator.py`，使测试通过
- test 阶段运行 `pytest` 确认全部绿灯