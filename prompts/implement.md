根据设计文档进行编码实现。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}

## 步骤

1. 确保涉及的所有服务仓库都在 `feature/{story_key}` 分支上（如不在则切换）
2. 如果存在设计文档，先阅读理解
3. 按设计文档在对应仓库中实现代码
4. 在每个仓库中提交改动（`git add` + `git commit`），提交信息用英文
5. 完成后记录修改的文件列表

## 完成后

将结果写入项目根目录下的 `.story-done/{story_key}/implement.json`：

```json
{
  "files_changed": ["改动的文件列表"],
  "summary": "实现了哪些功能"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks。