对设计方案进行质量审查。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}
{prd_path_section}

## 任务

审查 `design` 阶段的产出（技术方案、架构设计），检查：

1. 方案完整性：是否覆盖了需求中的所有功能点
2. 技术可行性：方案是否可落地，有无明显技术风险
3. 边界条件：异常情况、边界 case 是否考虑
4. 依赖关系：外部服务、数据库、API 的依赖是否明确

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
