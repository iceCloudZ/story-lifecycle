对需求进行分析与方案设计。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{prd_path_section}
{no_prd_section}
{skill_instruction}

## 步骤

{requirement_source}
分析需求范围，确定复杂度（S=小需求≤3文件, M=中等4-8文件, L=大需求>8文件或跨服务）和影响范围。
在开始设计前，在涉及的所有服务仓库中创建同名分支：
1. 确定需求涉及哪些服务仓库
2. 在每个相关仓库中，基于该仓库的主分支创建 `feature/{story_key}` 分支（如已有则切换到该分支）
3. 在该分支上进行后续工作
将设计文档写入项目 `docs/` 目录。

## 完成后

将结果写入项目根目录下的 `.story-done/{story_key}/design.json`：

```json
{
  "spec_path": "设计文档路径",
  "complexity": "S|M|L",
  "summary": "简要分析摘要"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks, no explanations. Pure JSON only — otherwise the system fails.

## 边界

- 只做分析和文档，写完 `.story-done/design.json` 就停止
- 不要安装依赖、不要修改代码、不要执行后续阶段