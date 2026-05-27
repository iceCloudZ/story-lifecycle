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

确定需求涉及的所有 git 仓库：
1. 扫描当前目录及子目录，找到所有 git 仓库（`git rev-parse --show-toplevel`）
2. 对每个仓库，分析是否需要修改代码
3. 将需要修改的仓库列在 `affected_repos` 中，包含仓库路径、名称、改动原因

将设计文档写入 `docs/` 目录。

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/design.json`：

```json
{
  "spec_path": "设计文档路径",
  "complexity": "S|M|L",
  "summary": "简要分析摘要",
  "affected_repos": [
    {
      "path": "仓库绝对路径",
      "name": "仓库名称",
      "reason": "需要做什么改动"
    }
  ]
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks, no explanations. Pure JSON only — otherwise the system fails.

## 边界

- 只做分析和文档，写完 `.story/done/design.json` 就停止
- **不要安装依赖、不要修改代码、不要创建分支**
- 不要执行后续阶段