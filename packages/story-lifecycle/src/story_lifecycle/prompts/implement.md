根据设计文档进行编码实现。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{spec_path_section}

## 前置检查（必须先执行）

1. 读取 `.story/done/{story_key}/design.json`，获取 `affected_repos` 列表
2. 如果 `affected_repos` 为空或不存在，**停止并报错**："design 阶段未指定 affected_repos，无法确定修改范围"
3. 对 `affected_repos` 中的每个仓库：
   - `cd` 到仓库路径
   - `git rev-parse --show-toplevel` 确认是 git 仓库（不是则报错停止）
   - `git checkout main && git pull`
   - `git checkout -b feature/{story_key}`（如分支已存在则 `git checkout feature/{story_key}`）

## 步骤

1. 阅读理解设计文档和 `design.json`
2. **仅在 `affected_repos` 列出的仓库中**修改代码
3. 不要修改 `affected_repos` 之外的任何仓库
4. 在每个仓库中提交改动（`git add` + `git commit`），提交信息用英文

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/implement.json`：

```json
{
  "files_changed": ["改动的文件列表"],
  "summary": "实现了哪些功能",
  "repos_modified": ["实际修改的仓库名称列表"]
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks.

## 边界

- **只能在 `affected_repos` 指定的 git 仓库中修改代码**
- **必须先创建/切换到 `feature/{story_key}` 分支**
- 不要修改非 git 仓库的文件
- 不要执行测试阶段的任务
