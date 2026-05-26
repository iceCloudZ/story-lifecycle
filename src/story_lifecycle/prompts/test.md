运行编译验证和测试。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}
{prd_path_section}

## 分支

确保在 `feature/{story_key}` 分支上操作。

## 步骤

1. 阅读设计文档了解需求范围
2. 确认当前在 feature 分支上，代码已正确修改
3. 对涉及的服务仓库分别运行编译命令，确认无编译错误
4. 如果有测试，运行冒烟测试
5. 记录编译和测试结果

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/test.json`：

```json
{
  "build_passed": true,
  "tests_passed": true,
  "summary": "编译和测试结果"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.