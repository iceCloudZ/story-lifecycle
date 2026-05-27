对实现代码进行质量审查。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}
{prd_path_section}

## 前置检查

1. 读取 `.story/done/{story_key}/design.json`，获取 `affected_repos` 列表
2. 读取 `.story/done/{story_key}/implement.json`，获取实际修改的文件列表

## 步骤

1. 阅读设计文档，理解需求范围和验收标准
2. 对 `affected_repos` 中的每个仓库：
   - `cd` 到仓库路径
   - `git diff main...feature/{story_key}` 查看所有改动
   - 审查以下维度：
     - 功能完整性：是否覆盖了设计文档中的所有需求
     - 代码质量：命名、结构、异常处理、边界条件
     - 安全性：是否有 SQL 注入、XSS、敏感信息泄露等风险
     - 向后兼容：是否破坏了已有接口和行为
3. 记录问题和改进建议

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/review.json`：

```json
{
  "quality": "pass|revise",
  "issues": [
    {"severity": "high|medium|low", "location": "文件:行号", "description": "问题描述"}
  ],
  "suggestions": ["改进建议"],
  "summary": "审查结论"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.

## 边界

- **只审查 `affected_repos` 列出的仓库的 `feature/{story_key}` 分支**
- 不要修改代码，只做审查
- 不要执行测试
