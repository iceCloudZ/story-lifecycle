> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# AI 研发工作流手动测试

本文档用于手工验证 `story-lifecycle` 驱动的 AI 研发流程。当前目标不是全自动开发，而是先把 PRD、阶段提示词、证据目录、context pack、gate 回填跑稳。

## 测试目标

- PRD 由后台 Intake 前置准备完成。
- AI 不在开发阶段生成 PRD。
- 证据统一落到 `D:/hc-all/story/<story-id>-<short-slug>/`。
- 阶段最小闭环为 `Design -> Build -> Verify`。
- 每个阶段都能人工确认，再推进下一步。
- 其他 AI 可以拿 context pack 继续回填。

## 前置条件

- `story-lifecycle` server 已启动。
- TAPD story 已同步到本地。
- story 已绑定相关项目仓库。
- PRD 已准备好，路径形如：

```text
D:/hc-all/story/1065618-授信提现展示拒绝原因/PRD.md
```

PRD 来源可以是：

- TAPD 详情正文整理而来。
- 钉钉文档下载/复制后整理而来。

## 建议测试 story

```text
story id: 1065618
主题: 授信/提现展示拒绝原因
story key: tapd-1144381896001065618
```

## 推荐执行通道

本轮优先用后台页面执行，CLI/API 只做复核和兜底。

- 后台页面：查看 story、复制阶段提示词、打开终端、人工推进阶段。
- CLI：确认当前阶段、手动推进、在后台不可用时做最小操作。
- API：验证 context、timeline、gate history、debug 信息是否真实回填。

常用命令：

```powershell
story serve
story list --all
story show tapd-1144381896001065618
story advance tapd-1144381896001065618
story done tapd-1144381896001065618
```

常用 API：

```powershell
$key = "tapd-1144381896001065618"
$base = "http://127.0.0.1:8180"

curl "$base/api/story/$key/context"
curl "$base/api/story/$key/context/pack"
curl "$base/api/story/$key/timeline"
curl "$base/api/story/$key/gate-history"
curl "$base/api/story/$key/debug"
```

## 手动测试流程

### 1. 确认 Intake

在后台打开 story 详情，确认：

- story 标题正确。
- 绑定项目正确。
- `PRD.md` 已存在。
- context 中有 `prd` document ref。
- 不存在乱码路径或 `????`。

若 PRD 不存在，先在后台完成 PRD 准备，不进入 CLI。

API 复核：

```powershell
curl "$base/api/story/$key/context"
```

期望：

- `documents` 中存在 `kind = prd`。
- `documents[].ref` 指向 `D:/hc-all/story/.../PRD.md`。
- `validation_errors = []`。
- `story.current_stage` 为 `design`，或后台明确显示可从 Design 开始。

### 2. 执行 Design

在后台复制 Design 阶段提示词，交给 AI 执行。

Design 阶段只允许：

- 读取 `PRD.md`。
- 扫描代码、grep、git、读取相关文件。
- 写 `research.md`。
- 写 `spec.md`。
- 用 `story-context` 回写 document refs 和 gate。

Design 阶段禁止：

- 修改代码。
- 创建分支。
- 调用 `prd-generator`。
- 把 PRD 写入业务仓库 `prd/`。

预期产物：

```text
D:/hc-all/story/1065618-授信提现展示拒绝原因/research.md
D:/hc-all/story/1065618-授信提现展示拒绝原因/spec.md
```

人工确认点：

- `research.md` 里记录了实际扫描命令。
- `spec.md` 的设计结论能回溯到 `research.md`。
- 复杂度 S/M/L 有理由。
- context validation errors 为空。

完成信号：

```text
<workspace>/.story/done/tapd-1144381896001065618/design.json
```

`design.json` 必须是纯 JSON，至少包含：

- `research_path`
- `spec_path`
- `complexity`
- `summary`
- `affected_repos`

人工确认通过后再推进：

```powershell
story advance tapd-1144381896001065618
```

### 3. 执行 Build

人工确认 Design 后，在后台推进到 Build，复制 Build 阶段提示词。

Build 阶段允许：

- 读取 PRD/research/spec。
- 写 `plan.md`。
- 按 affected repos 修改代码。
- 记录 DDL/Nacos 证据。
- 用 `story-context` 回写 plan、branch、change-items、build gate。

预期产物：

```text
D:/hc-all/story/1065618-授信提现展示拒绝原因/plan.md
D:/hc-all/story/1065618-授信提现展示拒绝原因/ddl.sql      # 有 DDL 时
D:/hc-all/story/1065618-授信提现展示拒绝原因/ddl.md       # 有或无 DDL 都建议记录结论
```

人工确认点：

- 代码只改 affected repos。
- 普通代码文件没有登记为 `code_ref`。
- DDL 没有随意放进服务仓库 `sql/`，除非确认它是正式发布脚本。
- gate 没有在缺少 evidence_ref 时写 PASS。

完成信号：

```text
<workspace>/.story/done/tapd-1144381896001065618/build.json
```

`build.json` 必须是纯 JSON，至少包含：

- `plan_path`
- `files_changed`
- `summary`
- `repos_modified`

人工确认通过后再推进：

```powershell
story advance tapd-1144381896001065618
```

若 Build 修改了代码，现场至少记录：

- 分支名和 worktree 路径。
- `git status --short`。
- 关键 diff 摘要。
- 是否涉及 DDL、Nacos、配置项、接口兼容性。

### 4. 执行 Verify

人工确认 Build 后，在后台推进到 Verify，复制 Verify 阶段提示词。

Verify 阶段负责：

- 编译验证。
- smoke/API/集成测试或人工验证记录。
- 整理 CI/MR/部署/发布证据。
- 生成 context pack。
- 回写 test-report、delivery、gate results。

预期产物：

```text
D:/hc-all/story/1065618-授信提现展示拒绝原因/test-report.md
D:/hc-all/story/1065618-授信提现展示拒绝原因/delivery.md
D:/hc-all/story/1065618-授信提现展示拒绝原因/context-pack.md
```

人工确认点：

- `test-report.md` 有真实验证命令或人工验证说明。
- `delivery.md` 有 CI/MR/部署证据。
- `context-pack.md` 能交给另一个 AI 继续理解上下文。
- context pack 没有普通代码文件索引污染。
- gate history 能看到真实证据。

完成信号：

```text
<workspace>/.story/done/tapd-1144381896001065618/verify.json
```

`verify.json` 必须是纯 JSON，至少包含：

- `test_report_path`
- `delivery_path`
- `context_pack_path`
- `build_passed`
- `tests_passed`
- `summary`

若验证只能人工完成：

- `test-report.md` 要写清楚人工验证步骤、环境、输入、输出、截图或日志位置。
- gate 写 `PARTIAL` 或 `BLOCKED`，不要写 `PASS`。
- `delivery.md` 写清楚剩余动作和负责人。

### 5. 收尾确认

Verify 人工确认通过后，才标记完成：

```powershell
story done tapd-1144381896001065618
```

最终复核：

```powershell
curl "$base/api/story/$key/context" | Out-File -Encoding utf8 context.json
curl "$base/api/story/$key/context/pack" | Out-File -Encoding utf8 context-pack.json
curl "$base/api/story/$key/timeline" | Out-File -Encoding utf8 timeline.json
curl "$base/api/story/$key/gate-history" | Out-File -Encoding utf8 gate-history.json
```

确认：

- `context.json` 中 `validation_errors` 为空。
- `context-pack.json` 中有 `PRD.md`、`research.md`、`spec.md`、`plan.md`、`test-report.md` 或交付证据。
- `timeline.json` 能看到 Design、Build、Verify 的关键事件。
- `gate-history.json` 的 PASS/PARTIAL/BLOCKED 都有证据引用。
- story 状态为 `completed`，TAPD 状态未被自动推进。

## Gate 结果手动回填

如果 AI 没有通过 `story-context` 正确回填 gate，可以用 API 手动补一条证据。只补事实，不替 AI 编造结果。

```powershell
$body = @{
  stage = "verify"
  gate_name = "manual-verify"
  result = "PARTIAL"
  summary = "已完成本地编译和接口 smoke，UAT 环境验证待补。"
  evidence_ref = "D:/hc-all/story/1065618-授信提现展示拒绝原因/test-report.md"
  evidence = @{
    command = "pytest"
    report = "D:/hc-all/story/1065618-授信提现展示拒绝原因/test-report.md"
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "$base/api/story/$key/gate-results" `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

结果约束：

- `PASS`：必须有真实 evidence_ref，且验证范围覆盖本阶段目标。
- `PARTIAL`：已有部分证据，但仍有明确缺口。
- `BLOCKED`：缺环境、缺依赖、缺权限或需求不明确，无法继续验证。
- `FAIL`：验证失败，需回到 Build 或重新设计。
- `WAIVED`：人工豁免，必须写清楚豁免人和原因。

## 最小验收清单

- 后台能看到 PRD/ref。
- Design prompt 能驱动 AI 写出 `research.md + spec.md`。
- Build prompt 能驱动 AI 写出 `plan.md` 并进行受控修改。
- Verify prompt 能驱动 AI 写出验证和交付证据。
- 每一阶段都可以人工确认后再推进。
- `context/pack` 可生成。
- `validation_errors = []`。
- 中文路径没有乱码。

## 常见失败与处理

| 现象 | 判断 | 处理 |
| --- | --- | --- |
| 后台看不到 PRD/ref | Intake 未完成或 context 未刷新 | 补 `PRD.md`，刷新 context，再复核 `/context` |
| 中文路径显示 `????` 或乱码 | 编码或终端输出问题 | 用 UTF-8 读取文件，确认数据库 ref 不是乱码 |
| Design 修改了代码 | 阶段边界失败 | 停止推进，记录问题，回滚或隔离该改动后重跑 Design |
| Build 修改了未绑定仓库 | affected repos 失控 | 停止推进，补充问题记录，重新确认 `design.json` |
| gate 为 PASS 但没有 evidence_ref | gate 回填不可信 | 改为 PARTIAL/BLOCKED，补 test-report 或 delivery 证据 |
| context pack 混入大量普通代码文件 | document/change-item 分类污染 | 删除错误 ref，只保留 PRD、research、spec、plan、DDL/Nacos、报告类证据 |
| `.story/done/*.json` 解析失败 | 完成文件不是纯 JSON | 删除 markdown fence 和解释文字，只保留 JSON |
| Verify 无法跑测试 | 验证环境缺口 | 写 `test-report.md`，gate 写 BLOCKED/PARTIAL，并列出缺口 |

## Debug 复核点

当后台状态和实际文件不一致时，优先看：

```powershell
curl "$base/api/story/$key/debug"
curl "$base/api/story/$key/timeline"
curl "$base/api/story/$key/gate-history"
```

需要确认：

- 最近事件里是否有 `validation_failure`、`node_error`、`gate_result_recorded`。
- timeline 中当前阶段和后台展示是否一致。
- gate history 中是否同时存在自动 gate 和人工补录 gate。
- 失败分支是否有 visible feedback，不要只有静默日志。

## 问题记录模板

```markdown
## 问题

- 阶段:
- 现象:
- 期望:
- 实际:
- 相关文件/接口:
- 是否阻塞:

## 判断

- 流程问题 / 后台问题 / skill 问题 / AI 执行问题 / 编码问题

## 处理建议

-
```

## 本轮不测试

- 全自动 AI 开发。
- 自动推进 TAPD 状态。
- 自动发布生产。
- 自动创建新 skill。
- 多 AI 并行回填。
