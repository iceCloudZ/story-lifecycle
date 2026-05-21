根据设计文档进行编码实现。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}

## 步骤

1. 如果存在设计文档，先阅读理解
2. 按设计文档实现代码
3. 完成后记录修改的文件列表

## 完成后

将结果写入项目根目录下的 `.story-done/implement.json`：

```json
{
  "files_changed": ["改动的文件列表"],
  "summary": "实现了哪些功能"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.
