对已准备好的 PRD 进行代码库调研与方案设计。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
- Story 证据目录: {story_dir}
{prd_path_section}
{skill_instruction}

## 步骤

1. 读取 PRD 文件。若 PRD 文件不存在或为空，停止并说明缺口，不要自行生成 PRD。
2. 扫描代码库，记录实际使用过的 `rg`、`git`、文件阅读等证据。
3. 将调研结论写入 `{story_dir}/research.md`。
4. 将设计方案写入 `{story_dir}/spec.md`。
5. 使用 `story-context` 回写 `research`、`spec` 文档引用和 gate 结果。

复杂度判定：

- S：单服务、小改动、无 DB/API 合约变化。
- M：单服务中等改动，或新增/调整接口但影响面可控。
- L：跨服务、DB/Nacos、金融核心参数、发布风险较高。

`research.md` 必须包含：

- PRD 摘要
- 扫描命令
- 相关仓库与模块
- 现有实现
- 影响面
- 未决问题
- 复杂度建议

## 完成后

将结果写入项目根目录下的 `.story/done/{story_key}/design.json`：

```json
{
  "research_path": "{story_dir}/research.md",
  "spec_path": "{story_dir}/spec.md",
  "complexity": "S|M|L",
  "summary": "简要分析摘要",
  "affected_repos": [
    {
      "path": "仓库绝对路径",
      "name": "仓库名称",
      "reason": "需要做什么改动"
    }
  ]
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks, no explanations. Pure JSON only — otherwise the system fails.

## 边界

- 只做分析和文档，写完 `.story/done/design.json` 就停止
- **不要安装依赖、不要修改代码、不要创建分支**
- 不要调用 prd-generator，不要把 PRD 写入业务仓库 prd/ 目录
- 不要执行后续阶段
