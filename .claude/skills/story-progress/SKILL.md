---
name: story-progress
description: 管家·进行中窗 — 查看活跃 story 进度态势、确认门、下一步该干嘛,并安全推进非终态确认门。Use when user asks 进度/态势/现在哪些 story/进行中/下一步该干嘛/把 X 推了(非终态)。终态/发布类确认走 story-review skill,排查卡住走 story-patrol skill。
---

# Story Progress — 管家·进行中窗

管家三窗之一(进行中/排查/评审,DESIGN-story-butler §3.6)。职责:活跃 story 的态势问答与非终态确认门操作。

## 前置

- serve 必须在跑(`run-story-serve` skill 启动);健康检查 `curl http://127.0.0.1:8180/api/session/health`
- MCP 工具面(`butler_server` 的 `story_list`/`story_detail`/`story_advance`/`session_register`)应可用;不可用时降级为直接 curl serve REST API(工具的语义映射见下),别干等

## 标准动作

1. **态势**:`story_list` → 按"等你决策(停门)> 活跃 > 其他"排序汇报;每条带 key/标题/状态/阶段/是否停门
2. **深入**:`story_detail(key)` → judge 摘要、确认门 targetState、证据指针、下一步建议
3. **推进(非终态)**:`story_advance(key, confirm_token)` —— **confirm_token 必须拼 `"<story_key>:<target_state>"`,targetState 以 story_detail 返回为准**;工具会拒绝错的拼法和一切终态目标,被拒时读返回里的提示自纠,别绕过
4. **会话簿记**:开始跟进某 story 时顺手 `session_register(key, stage, adapter="zcode", session_id=<本会话 id>)` —— 断点续跑有据可查

## 纪律(违反即翻车)

- **决定前重读**:每次回答前用工具拉最新数据,**不信会话记忆里的旧状态**(常驻窗口的上下文是旧的;DB 是唯一事实源)
- **上下文不串台**:多个 story 混聊时,结论逐 story 归档;引用数据必须来自当次的工具返回
- **终态一律转出**:上线/结项类目标 → 明确告诉用户"这是终态确认,请在评审窗(story-review skill)或 Story 详情页 UI 以完整上下文做",本窗不碰
- 推进成功后报告 `confirmed_via` 落账情况(默认 desktop)
