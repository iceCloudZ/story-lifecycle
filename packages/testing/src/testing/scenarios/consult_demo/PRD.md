# PRD: trivial greeter + 强制 consult

> 本 PRD 是 consult 真 E2E 验收用的人造场景。需求本身极简(greeter 函数),
> 但**强制要求** code agent 在 implement 阶段必须调 `story consult` 求助一次
> (无论问题难易)——这是为了在真实 headless stage 内端到端验证 consult 链路。

## 概述

实现一个最小化的 Python greeter 模块,提供单个函数 `greet(name) -> str`。

## 功能需求

### `greet`

在 `greeter.py` 中提供函数:

```python
def greet(name: str) -> str:
    """Return a friendly greeting for the given name."""
    ...
```

行为:
- `greet("World")` 返回 `"Hello, World!"`
- `greet("")` 返回 `"Hello, stranger!"`(空名兜底)
- 其他非空 name 返回 `"Hello, <name>!"`

## 验收标准

- 预置测试 `tests/test_greeter.py` 全部通过

## **强制 consult 协议(本次验收的关键)**

在 implement 阶段,**你必须在写代码之前**用 Bash 工具运行 `story consult` 一次,
咨询编排层关于「greet 函数的最佳实现风格(单行 f-string vs 多行 if/else)」的意见。
具体调用:

```bash
story consult --question "greet 函数应该用单行 f-string 还是多行 if/else?为什么?" --urgency medium
```

这一步**不能跳过**——它是本次验收的核心断言点。consult 完成后,你可以采纳或
不采纳编排层的建议(在 done summary 里说明理由即可),然后继续写代码。

**纪律提醒**:能自己查清楚的不该 consult;但本 PRD 把它定为强制步骤,是为了
验证 consult 链路本身。真实场景里 consult 只在卡住时用。
