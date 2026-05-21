调用 /brainstorming skill 进行需求分析与方案设计。

## Story 信息

- Story Key: {story_key}
- 标题: {title}
{prd_path_section}

## 完成后

将分析结果写入**项目根目录**下的 `.story-done/design.json`：

```json
{
  "spec_path": "docs/specs/STORY-XXX-design.md",
  "complexity": "S|M|L",
  "affected_services": ["hc-user"],
  "summary": "简要分析摘要"
}
```

> 文件必须只包含纯 JSON，不要用 markdown 代码块包裹。
> 系统会自动检测该文件并推进到下一阶段。

## 边界

- 完成后写 `.story-done/design.json` 然后停止
- **不要执行后续任何阶段**
