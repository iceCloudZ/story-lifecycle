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