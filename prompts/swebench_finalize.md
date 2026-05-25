生成最终 patch 用于 SWE-bench 评估。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{prd_path_section}

## 步骤

1. 运行 `git diff` 查看当前所有改动
2. 确认 diff 只包含修复核心逻辑所需的改动
3. 如果 diff 包含无关文件（日志、临时文件、本地配置、格式化无关改动、依赖缓存、测试产物），必须先清理
4. 运行 `git diff --stat` 确认 diff 干净

## 完成后

将结果写入项目根目录下的 `.story-done/{story_key}/finalize.json`：

```json
{
  "model_patch": "完整的 git diff 输出",
  "patch_summary": "一句话描述修复内容"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks。
> CRITICAL: model_patch 只包含修复核心逻辑所需的 diff，不要包含任何无关改动。
