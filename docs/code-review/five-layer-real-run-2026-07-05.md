# 五层决策真跑报告
_真 deepseek(model=deepseek-v4-pro) + 真 repo 内容(.story/done、event_log、story 表)_


## 层1 执行内 — supervisor

### decide_response(真 deepseek,真澄清问题)
- 问题: release gate 失败:分支没合并 master、DDL 没确认。先做哪个? A) 先合并分支 B) 先确认 DDL
- 决策: choice=`A` reason=`合并分支是发布就绪前提，代码未入主干则DDL无法评估可行性`

### supervise_pty_session 闭环(真 winpty + 真 deepseek + 真 db.log_event)
- PTY 输出(ansi-stripped 末尾): '正在分析 story...\r\n请选择: A) 合并分支 B) 先确认 DDL\r\nB\r\nRECEIVED_ANSWER:B\r\n'
- 含 RECEIVED_ANSWER: True
- supervisor_decision 事件: 1 条(id=331)

## 层3 异常 — recovery(规则驱动,0 LLM)

### decide_recovery 在真 node_error(401 鉴权失败)上
- 真失败: 401 鉴权(deepseek API)
- 决策: action=`escalate_human` reason=`auth/config 类错误(RuntimeError),重试无价值 → 上交人处理`
- 瞬时失败(done 永不现): action=`retry_new_adapter` new_adapter=`claude`

## 层4 评判 — judge(真 deepseek,真 implement.json)
- [tapd-1144381896001065315] pass=`True` rework=`None` reason=`实现替换了硬编码账号为动态配置，覆盖前端Dashboard、状态存储和后端API，变更完整且通过硬指标检查，质量达标。`
- [tapd-1144381896001065346] pass=`True` rework=`None` reason=`实现覆盖了数据库、后端API和前端页面的完整功能，硬指标通过，无明显缺陷`
- [tapd-1144381896001065458] pass=`True` rework=`None` reason=`实现覆盖了需求提及的数据表、索引、CRUD、API及前端页面，结构完整，构建和测试通过，无明显缺陷。`

## 层2 边界 — transition(规则驱动,0 LLM)

### decide_transition 在真 gate_result_recorded FAIL 上
- 真 gate FAIL → action=`retry` reason=`可恢复失败(quality)→ 同 stage 重试(2/2)`
- replanner build_replan_messages(真 gate 反馈):
```
失败的 stage: release
失败模式: quality
失败原因: 分支未合并 master、DDL 未确认
已试过(别重复): release@claude
请给出修订后的计划。
```

## 层5 元 — reflection + scheduler
- reflect 在真 event_log(328 条近期事件): stats={'supervisor_decision': 3, 'gate_result_recorded': 141, 'context_pack_generated': 94, 'route_decision': 8, 'node_error': 16, 'execute': 8, 'prompt_context': 8, 'planner_blocked': 8, 'completed': 11, 'bugfix_prompt_generated': 14, 'release_prompt_generated': 11, 'batch_bugfix_prompt_generated': 2, 'post_release_prompt_generated': 4} playbook 条数=0
- (playbook 空 = 真 event_log 还没 recovery_action→pass 链;新 wired,真 story 尚未触发)

### decide_schedule 在真 idle story(123 个)上
- 真排序后前 5:
  1. tapd-bug_1144381896001006394  priority=`high`
  2. tapd-bug_1144381896001006433  priority=`high`
  3. tapd-bug_1144381896001006438  priority=`high`
  4. tapd-bug_1144381896001006441  priority=`high`
  5. tapd-bug_1144381896001006442  priority=`high`
- ... 共 123 个 idle story 排序

## 真测发现
- **scheduler priority 格式**:真 DB 用 high/medium/low(非 P0-P5);已修 `_PRIORITY_RANK` 支持两者(否则全当 P2 退化 FIFO)。
- **真 event_log 无 recovery_action/judge_verdict/transition_decision 事件**:这三层新 wired,真 story 还没真触发过(需经 story serve 跑失败/verify 才会落)。
- **recovery 在真 401 鉴权失败上正确判 escalate_human**(auth/config 类不浪费重试)。
- **judge 在 3 个真 shipped 实现上判 pass**(reason 实质;空 stub 会判 rework 已另验)。