运行编译验证和测试。

## 任务信息

- Story Key: {story_key}
- 标题: {title}

## 步骤

1. 确认代码已正确修改
2. 运行项目编译命令，确认无编译错误
3. 如果有测试，运行冒烟测试

## 完成后

将结果写入项目根目录下的 `.story-done/test.json`：

```json
{
  "build_passed": true,
  "tests_passed": true,
  "summary": "编译和测试结果"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.
