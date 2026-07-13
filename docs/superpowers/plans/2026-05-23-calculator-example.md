# 示例项目：表达式计算器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建 `examples/calculator/` 示例项目，让用户快速体验 design → implement → test 三步流程。

**Architecture:** 纯文件创建，不修改 story-lifecycle 核心代码。包含 PRD（需求文档）、预置测试用例（红灯状态）和 README。

**Tech Stack:** Python 3.10+, pytest

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `examples/calculator/PRD.md` | 产品需求文档，AI 在 design 阶段阅读 |
| Create | `examples/calculator/tests/__init__.py` | 空文件，使 tests 成为可导入包 |
| Create | `examples/calculator/tests/test_calculator.py` | 预置测试用例，覆盖所有需求 |
| Create | `examples/calculator/README.md` | 快速开始指南 |

---

### Task 1: 创建 PRD.md

**Files:**
- Create: `examples/calculator/PRD.md`

- [ ] **Step 1: 创建目录并写入 PRD**

```bash
mkdir -p examples/calculator/tests
```

```markdown
# PRD：表达式计算器

## 概述

实现一个轻量级的表达式计算器库，提供基础数学运算和链式调用能力。

## 功能需求

### 静态方法

在 `Calculator` 类中提供以下静态方法：

| 方法 | 签名 | 说明 |
|------|------|------|
| `add` | `add(a, b) -> float` | 加法 |
| `subtract` | `subtract(a, b) -> float` | 减法 |
| `multiply` | `multiply(a, b) -> float` | 乘法 |
| `divide` | `divide(a, b) -> float` | 除法，`b=0` 时抛 `ZeroDivisionError` |
| `power` | `power(base, exp) -> float` | 幂运算，支持负数指数 |
| `mod` | `mod(a, b) -> float` | 取模，`b=0` 时抛 `ZeroDivisionError` |

### 链式调用

`Calculator()` 创建实例，初始值为 0。支持链式操作：

| 方法 | 说明 |
|------|------|
| `add(v)` | 累加 |
| `subtract(v)` | 累减 |
| `multiply(v)` | 乘到当前值 |
| `divide(v)` | 除当前值，`v=0` 时抛 `ZeroDivisionError` |
| `result()` | 返回当前累计值（float） |

示例：`Calculator().add(5).subtract(2).multiply(3).result() == 9`

## 技术约束

- 纯 Python 实现，无第三方依赖
- 代码放在 `calculator.py`，类名为 `Calculator`
- 除零和取模零必须抛 `ZeroDivisionError`
- 返回值类型为 `float`

## 验收标准

- 所有预置测试（`tests/test_calculator.py`）通过
```

- [ ] **Step 2: 提交**

```bash
git add examples/calculator/PRD.md
git commit -m "docs: add calculator example PRD"
```

---

### Task 2: 创建测试文件

**Files:**
- Create: `examples/calculator/tests/__init__.py`
- Create: `examples/calculator/tests/test_calculator.py`

- [ ] **Step 1: 创建 `tests/__init__.py`**

空文件。

- [ ] **Step 2: 创建 `tests/test_calculator.py`**

```python
import pytest

from calculator import Calculator


class TestStaticMethods:
    """静态方法：四则运算、幂、取模"""

    def test_add(self):
        assert Calculator.add(2, 3) == 5

    def test_add_negative(self):
        assert Calculator.add(-1, 1) == 0

    def test_subtract(self):
        assert Calculator.subtract(5, 3) == 2

    def test_subtract_negative_result(self):
        assert Calculator.subtract(1, 5) == -4

    def test_multiply(self):
        assert Calculator.multiply(4, 3) == 12

    def test_multiply_by_zero(self):
        assert Calculator.multiply(5, 0) == 0

    def test_divide(self):
        assert Calculator.divide(10, 2) == 5

    def test_divide_non_integer(self):
        assert Calculator.divide(7, 2) == 3.5

    def test_divide_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            Calculator.divide(1, 0)

    def test_power(self):
        assert Calculator.power(2, 3) == 8

    def test_power_negative_exponent(self):
        assert Calculator.power(2, -1) == 0.5

    def test_power_zero_exponent(self):
        assert Calculator.power(2, 0) == 1

    def test_mod(self):
        assert Calculator.mod(10, 3) == 1

    def test_mod_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            Calculator.mod(10, 0)


class TestChaining:
    """链式调用"""

    def test_basic_chain(self):
        assert Calculator().add(5).subtract(2).multiply(3).result() == 9

    def test_add_chain(self):
        assert Calculator().add(2).add(3).result() == 5

    def test_divide_chain(self):
        assert Calculator().add(10).divide(2).result() == 5

    def test_chain_starts_at_zero(self):
        assert Calculator().result() == 0

    def test_chain_divide_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            Calculator().add(5).divide(0)
```

- [ ] **Step 3: 验证测试红灯**

```bash
cd examples/calculator && python -m pytest tests/ -v
```

Expected: 所有测试 FAIL（`ModuleNotFoundError: No module named 'calculator'`），确认测试正确依赖未实现的代码。

- [ ] **Step 4: 提交**

```bash
git add examples/calculator/tests/
git commit -m "test: add pre-written calculator tests (red)"
```

---

### Task 3: 创建 README.md

**Files:**
- Create: `examples/calculator/README.md`

- [ ] **Step 1: 创建 README**

```markdown
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
```

- [ ] **Step 2: 提交**

```bash
git add examples/calculator/README.md
git commit -m "docs: add calculator example README"
```

---

## Self-Review

- **Spec coverage:** PRD ✓ 预置测试 ✓ README ✓ `__init__.py` ✓
- **Placeholders:** 无 TBD/TODO
- **Type consistency:** 所有测试用例中 `Calculator` 类名、方法名与 PRD 一致
- **Anti-tampering:** 不适用（无核心参数）
