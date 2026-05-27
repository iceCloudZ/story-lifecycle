对设计方案进行质量审查。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}
{prd_path_section}

## 前置检查

1. 读取 `.story/done/{story_key}/design.json`，获取设计方案和 `affected_repos` 列表
2. 阅读设计文档

## 审查维度

1. **需求覆盖**：方案是否覆盖了 PRD 中的所有功能点
2. **仓库完整性**：`affected_repos` 是否遗漏了需要修改的仓库
3. **技术可行性**：方案是否可落地，有无明显技术风险
4. **边界条件**：异常情况、边界 case 是否考虑
5. **依赖关系**：外部服务、数据库、API 的依赖是否明确

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/review_design.json`：

```json
{
  "quality": "pass|revise",
  "issues": [
    {"severity": "high|medium|low", "location": "设计文档章节", "description": "问题描述"}
  ],
  "suggestions": ["改进建议"],
  "summary": "审查结论"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.

## 边界

- 只读：不要修改代码、不要创建分支
- 如果发现 `affected_repos` 不完整，在 issues 中标注
