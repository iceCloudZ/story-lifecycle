执行验证并整理交付证据。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
- Story 证据目录: {story_dir}
{prd_path_section}
{spec_path_section}
{transcript_context}
{knowledge_context}

{quality_checklist}

## 步骤

1. 读取 `{story_dir}/PRD.md`、`research.md`、`spec.md`、`plan.md`。
2. 按 plan/spec 执行编译、smoke、集成测试或人工验证。
3. 将验证结果写入 `{story_dir}/test-report.md`。
4. 将 CI、MR、Skyladder、部署、发布准备结论写入 `{story_dir}/delivery.md`。
5. 生成或刷新 `{story_dir}/context-pack.md`。
6. 使用 `story-context` 回写 test-report、delivery artifacts、gate results 和 context-pack。

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/verify.json`：

```json
{
  "test_report_path": "{story_dir}/test-report.md",
  "delivery_path": "{story_dir}/delivery.md",
  "context_pack_path": "{story_dir}/context-pack.md",
  "build_passed": true,
  "tests_passed": true,
  "summary": "验证和交付证据摘要"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.

## 边界

- 没有真实 evidence_ref 时不要把 gate 写成 PASS。
- 如果验证依赖人工确认，gate 写 PARTIAL 或 BLOCKED，并说明缺口。
- 不要继续做发布后动作。
