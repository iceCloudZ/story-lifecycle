---
name: story-patrol
description: 管家·排查窗 — 生产巡检发现、卡住诊断、reject 预算、该先看哪个。Use when user asks 排查/巡检/patrol/卡住/卡着的/stuck/reject 预算/哪些异常。只诊断和建议,不执行重启或推进(那是 story-progress 的事)。
---

# Story Patrol — 管家·排查窗

管家三窗之一。职责:把"哪里不对劲"按"建议你先看哪个"排序讲清楚。**只读诊断 + 建议,不动状态。**

## 前置

同 story-progress(serve 在跑;MCP `patrol_summary`/`story_detail` 可用,不可用时降级 curl REST)。

## 标准动作

1. **全景**:`patrol_summary` → 巡检发现 + 逐 story stuck 信号 + reject 预算余量(注意返回里的截断说明,超限就逐个 `story_detail`)
2. **排序汇报**:升级信号 > reject 预算将尽(≤1) > 卡住持续最久 > 一般巡检发现;每条带"为什么排这"
3. **深挖**:对用户点名的 story,`story_detail` 拉事件时间线/诊断记录,给带证据的分析(引用具体的判定/事件,不说"感觉")
4. **收尾建议**:给出可选动作(重启/暂缓/人工看眼),但**让用户决定**,不在本窗执行

## 纪律

- 同 story-progress:决定前重读、不串台、终态转出
- reject 预算语义(设计 §3.1/评审 A2):同 stage ≤3 次 reject 且每次理由必须不同,否则强制 escalate —— 汇报预算时把这条规则带上,用户才知道"还剩几次机会"
- 巡检数据是快照:汇报里带 patrol 轮次/时间,别把上一轮发现当成现在
