---
scenario: order.withdraw
domain: order
title: 提现
status: proposed
confidence: medium
source_refs:
  - type: code
    ref: hc-order/path/to/Controller.java
  - type: table
    ref: hc_order.t_order
last_generated_at: 2026-06-01T00:00:00+08:00
last_verified_by:
last_verified_at:
---

# 提现

## 1. 场景概述

描述这个业务场景解决什么问题，用户从哪里进入，最终期望状态是什么。

## 2. 业务主流程

1. 用户触发入口。
2. 服务校验前置条件。
3. 写入或更新核心数据。
4. 调用下游服务、MQ 或三方。
5. 状态完成或进入异常分支。

## 3. 涉及服务和模块

| 服务 | 模块 | 作用 | 来源 |
| --- | --- | --- | --- |
| hc-order | order | 订单创建和状态流转 | `source-ref` |

## 4. 用户入口/API入口

| 入口 | 类型 | 说明 | 来源 |
| --- | --- | --- | --- |
| `/api/v1/...` | API | 入口说明 | `source-ref` |

## 5. 核心代码路径

- `path/to/Class.java#method` - 说明。

## 6. 数据交互逻辑

说明主要数据如何从入口传入、被转换、写入表、传给下游。

## 7. 核心表和关键字段

| 表 | 字段 | 含义 | 生成/更新位置 | 来源 |
| --- | --- | --- | --- | --- |
| `db.table` | `field` | 待确认 | `source-ref` | `source-ref` |

## 8. MQ / Feign / Redis / OSS / 三方依赖

| 类型 | 名称 | 方向 | 说明 | 来源 |
| --- | --- | --- | --- | --- |
| MQ | topic/tag | publish | 说明 | `source-ref` |

## 9. 状态机和关键状态

列出状态、流转条件、终态和异常态。

## 10. 异常分支

- 前置条件失败。
- 下游失败。
- MQ 重复/丢失/延迟。
- 回调状态不一致。

## 11. 历史 bug 和风险点

| Bug | 风险模式 | 影响 | 回归要求 | 来源 |
| --- | --- | --- | --- | --- |
| 待补充 | 待补充 | 待补充 | 待补充 | `source-ref` |

## 12. 回归测试清单

- [ ] 主流程成功。
- [ ] 前置条件失败。
- [ ] 重复提交/幂等。
- [ ] 下游失败。
- [ ] 状态和表字段一致。

## 13. 生产问题排查路径

1. 根据用户/订单定位核心表。
2. 检查状态机当前状态。
3. 检查 MQ/三方/回调。
4. 检查历史 bug 风险点。

## Pending Review

- 尚无证据或需要人工确认的结论。
