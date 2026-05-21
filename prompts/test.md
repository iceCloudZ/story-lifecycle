运行编译验证和冒烟测试。

## Story 信息

- Story Key: {story_key}
- 标题: {title}

## 完成后

将结果写入 `.story-done/test.json`：

```json
{
  "build_passed": true,
  "tests_passed": true,
  "summary": "编译和测试结果"
}
```

> 文件必须只包含纯 JSON。
