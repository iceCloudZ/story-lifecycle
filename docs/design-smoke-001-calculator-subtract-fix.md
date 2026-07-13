# Design: smoke-001 — calculator.py subtract 函数修复

## 复杂度

**S**（小需求，≤3 文件）

## 影响范围

| 文件 | 变更类型 |
|------|----------|
| `calculator.py` | 修复 `subtract` 函数实现 |
| `test_calculator.py`（如存在） | 确认 `test_subtract` 通过 |

## 问题分析

`calculator.py` 的 `subtract(a, b)` 函数当前实现为加法（`a + b`），应改为减法（`a - b`）。

## 修复方案

定位 `subtract` 函数体，将 `+` 运算符替换为 `-`。

```python
# Before (bug)
def subtract(a, b):
    return a + b

# After (fix)
def subtract(a, b):
    return a - b
```

## 验证

运行 `test_subtract` 用例，确认：
- `subtract(5, 3) == 2`
- 边界情况如 `subtract(0, 0) == 0`、`subtract(-1, -1) == 0` 通过

## 风险

无。单行修改，逻辑明确，无副作用。
