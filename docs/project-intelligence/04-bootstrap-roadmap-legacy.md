# Idea: Project Intelligence Bootstrap Roadmap

## 背景

`03-bootstrap-design.md` 已经确定核心方向：先在本地通过 Prompt/CLI-first 生成 `.story/knowledge/`，让 Story Lifecycle 在进入 story 阶段前能构建可审计的项目上下文。这个 idea 文档用于记录后续演进计划，避免后续实现时又滑向重扫描器、向量库或远程平台先行。

核心判断：

- 先本地跑通，再远程化。
- 先 Prompt/CLI 生成，再沉淀工具。
- 先文件协议和 context packet，再做数据库/API。
- 先证据可追溯，再追求自动化程度。

## 目标状态

最终希望形成这条链路：

```text
story project init-knowledge
  -> CLI headless 生成 .story/knowledge
  -> story project sync-knowledge 做增量更新和 stale 检测
  -> stage 前生成 knowledge-context packet
  -> Planner 基于 packet 生成任务书
  -> Executor 带项目知识执行
  -> 本地 skill/test/bug/release 事件反哺 knowledge
  -> 稳定后同步到 ys-agent
```

## P1: 本地 Bootstrap 跑通

目标：用户能在任意项目根目录运行一次命令，生成 `.story/knowledge/`。

范围：

- 新增 `story project init-knowledge` 命令。
- 使用 CLI headless 执行 `bootstrap-prompt-template.md`。
- 生成 `.story/knowledge/product.yaml`、`manifest.yaml`、`search-catalog.md`。
- 生成场景、索引、graph、pending review 的最小可用产物。
- 写入 `.story/done/PROJECT-KNOWLEDGE-INIT/knowledge_bootstrap.json`。
- 校验关键文件存在，不校验内容完整度。

P1 还必须补齐：

- `story project sync-knowledge` 的轻量版本。
- stale 检测：commit、dirty 状态、关键文件 mtime。
- 结构化 Search Tool 的最小版本，避免 LLM 直接拼 shell。

不做：

- 不实现 AST parser。
- 不实现向量库。
- 不接 `ys-agent`。
- 不要求场景文档全量准确。

验收：

- 在 HappyCash 类 Java/Spring 项目上能生成第一版 `.story/knowledge/`。
- 至少包含一个 domain、一个 scenario、一个 index、一个 graph JSON。
- 关键结论有 `source_refs` 或进入 `pending-review-items.md`。

## P2: Context Packet 注入

目标：每个 story/stage 开始前，可以生成精简的知识上下文包。

范围：

- 新增 Context Builder 调用点。
- CLI headless 执行 `context-builder-prompt-template.md`。
- 读取 `.story/knowledge/manifest.yaml`、`search-catalog.md`、`product-context-graph.json`。
- 按 describe/search/expand/compose 四步生成：
  - `.story/context/<story_key>/knowledge-context/<stage>.md`
  - `.story/context/<story_key>/knowledge-context/<stage>.json`
- Planner 在生成阶段任务书时读取 context packet。

关键约束：

- packet 是注入物，不是完整知识包。
- packet 必须解释为什么选择这些上下文。
- 关键结论必须有 source refs。
- `proposed` 内容必须标记待确认。

后续可补：

- token budget 控制。
- 舍弃节点记录。
- stage-specific context 策略。

验收：

- 对一个提现/授信/还款类 story，可以生成相关场景、服务、表、bug、测试点的 context packet。
- Planner 输出任务书时能引用 packet。

## P3: 稳定步骤工具化

目标：把 CLI 中反复稳定执行的步骤沉淀成本地 helper，提高速度和可控性。

优先工具化：

- `describe_project_knowledge`
- `search_project_knowledge`
- `expand_project_context`
- `compose_context_packet`

顺序：

1. 先工具化 Search Tool。
2. 再工具化 graph expand。
3. 最后工具化 compose 的预算和裁剪。

保留：

- CLI fallback。
- prompt override。
- 文件协议不变。

验收：

- Search 不依赖 LLM 拼 shell。
- graph expand 能从 scenario 扩展到 service/api/table/mq/bug/test。
- context packet 大小可控。

## P4: 本地事件反哺

目标：把本地 skill 和 story 执行过程产生的事件写回知识演进链路。

事件来源：

- `knowledge-capture` -> `knowledge.captured`
- `bug-track` -> `bug.recorded`
- `test-runner` -> `test.completed` / `test.failed`
- `pre-release-review` -> `release.reviewed`
- `product-health-monitor` -> `inspection.suggested` / `inspection.failed`

本地落点：

```text
.story/knowledge/events/local-skill-events.jsonl
```

输出：

- knowledge update suggestion
- bug risk update
- regression checklist update
- inspection suggestion
- pending review item

验收：

- 一个 bug 记录能生成对应的知识更新建议。
- 一个测试失败能关联到场景、服务、表或 bug 风险。

## P5: 远程化到 ys-agent

目标：把本地稳定的 Knowledge Pack 发布到公司级平台。

`story-lifecycle` 负责：

- 本地生成。
- 本地验证。
- 本地事件导出。
- 本地 context packet。

`ys-agent` 负责：

- Knowledge Pack registry。
- 审核和发布。
- 公司级 Skill。
- 权限、审计、版本管理。
- 多项目知识包管理。

远程 source 规则：

- 只认 Git repo + commit。
- 不把本地路径作为正式 source。
- 本地 `.story/knowledge` 可以作为草稿或导出包，但远程发布必须绑定 Git commit。

验收：

- HappyCash Knowledge Pack 能上传或同步到 `ys-agent`。
- 测试助手能基于已发布 pack 回答，并引用证据。

## P6: 语义检索和 GraphRAG

触发条件：

- 精确搜索和 graph expand 无法覆盖概念相似问题。
- 代码/业务命名不稳定，关键词召回明显不足。
- context packet 选择质量不足。

可引入：

- embedding。
- rerank。
- GraphRAG。
- 相似 bug 检索。
- 多项目跨依赖检索。

原则：

- 语义检索是增强，不替代 evidence-first。
- 向量命中不能单独作为关键结论，必须回到 source refs。

## 风险和控制

### 知识腐烂

风险：代码变更后知识包过期。

控制：

- P1 做 `sync-knowledge`。
- context builder 前检查 stale。
- stale 时强提醒。

### Search 空转

风险：LLM 生成错误关键词或错误正则。

控制：

- P1 提供结构化 Search Tool。
- keyword、type、target_paths 参数化。
- 工具内部做路径限制、转义、截断。

### Context 爆炸

风险：graph 多跳扩展过多。

控制：

- P2/P3 加 token budget。
- 记录裁剪和舍弃节点。
- stage-specific 优先级。

### 隐式依赖遗漏

风险：AOP、动态 Feign、反射、运行时配置无法扫描。

控制：

- `declarations/manual-context.yaml`
- `declarations/critical-flows.yaml`
- 人工声明优先于 AI 推断。

## 最近可执行的下一步

1. 实现 `story project init-knowledge` 命令骨架。
2. 复用现有 adapter/CLI headless 能力启动 bootstrap prompt。
3. 只做文件存在校验和 done JSON 校验。
4. 在 HappyCash 工作区试跑一次。
5. 根据试跑结果调整 prompt 模板。
6. 再实现 `sync-knowledge` 的 stale 检测。

## 成功标准

第一阶段成功不是“知识图谱完整”，而是：

- 本地 AI 在做 story 前能拿到项目级知识上下文。
- 生成的上下文有证据、可审计、不过度膨胀。
- 过期时能提醒。
- 真实项目试跑后，AI 的方案和测试建议明显更贴近业务。
