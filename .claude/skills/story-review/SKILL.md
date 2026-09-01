---
name: story-review
description: 管家·评审窗 — 确认门的正式评审场所:呈报证据(judge 摘要/成果物/测试报告)、给出建议、人拍板后执行推进;唯一允许处理终态/发布类确认的地方。Use when user asks 评审/确认门/推不推/能不能上线/终态/结项/发布。非终态日常推进走 story-progress。
---

# Story Review — 管家·评审窗

管家三窗之一,**确认门的唯一正式评审场所**(DESIGN-story-butler §3.2/§3.6)。

## 前置

同 story-progress(serve 在跑;MCP `story_detail`/`story_advance` 可用,不可用时降级 curl REST)。

## 评审流程(每个确认门都走全,不跳步)

1. **呈报**:`story_detail(key)` → 完整呈报四件套:**judge 结论与理由 / 证据指针(spec、test-report、git 变更)/ 目标态与影响 / 建议与风险**。证据没看全不许建议
2. **等拍板**:明确问用户"推/不推/要先看什么"。用户不答不推进;用户追问就补充呈报
3. **执行**:
   - 非终态 → `story_advance(key, confirm_token="<key>:<targetState>")`
   - **终态(上线/结项)→ MCP 工具会拒绝(设计如此)。终态的合法通道是 Story 详情页 UI 的确认按钮(终态挂起门走 `/lifecycle/ui-upgrade`)——引导用户去 UI 完成最后一步,本窗呈报证据但不在对话里按终态按钮**
4. **落账回执**:推进成功后回报 confirmed_via;失败的 409/428 原文转述给用户(那是 serve 的门在执法,不是故障)

## 纪律

- **决定准备 vs 决定**:本窗做决定准备(证据+建议),**人做决定** —— 这个分工不能倒,agent 的"建议推"永远只是建议
- 微信里发来的同一确认(如果用户手机上已回过 T):先查 DB 事件确认门是否已被推过,**已推过就别重复推**(confirmed_via=wechat 的记录查得到)
- 高危三问(终态评审必答):回滚方案?影响面?为什么是现在?
