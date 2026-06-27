根据 PRD、research 和 spec 制定实施计划并完成编码实现。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
- Story 证据目录: {story_dir}
{prd_path_section}
{spec_path_section}
{transcript_context}

## 前置检查

1. 读取 `.story/done/{story_key}/design.json`。
2. 读取 `{story_dir}/research.md` 和 `{story_dir}/spec.md`。
3. 如果 `affected_repos` 为空或缺失，停止并说明 design 阶段证据不足。

## 步骤

1. 将实施计划写入 `{story_dir}/plan.md`。
2. 按 plan 在 `affected_repos` 指定的仓库中修改代码。
3. 涉及 DDL/Nacos 时，把证据写到 `{story_dir}/ddl.sql` / `{story_dir}/ddl.md`，并用 `story-context` 回写 change-item。
4. 使用 `story-context` 回写 `plan` 文档引用、分支绑定和 build 阶段 gate。

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/build.json`：

```json
{
  "plan_path": "{story_dir}/plan.md",
  "files_changed": ["改动的文件列表"],
  "summary": "实现了哪些功能",
  "repos_modified": ["实际修改的仓库名称列表"]
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.

## 边界

- 不要修改 `affected_repos` 之外的业务仓库。
- 不要把普通代码文件登记为 `code_ref`。
- 不要执行 verify 阶段的测试报告、delivery 或 context-pack 收尾。
