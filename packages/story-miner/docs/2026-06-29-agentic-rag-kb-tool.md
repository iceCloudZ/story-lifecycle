# Agentic RAG — kb.py 查询工具（让执行器 agent 自己检索知识）

> 2026-06-29 · 把 story-lifecycle 从"规则 RAG（预注入）"升级到"agentic RAG（agent 按需查）"。

## 动机

现在知识是 prompt 渲染时**按 task_type 一次性预注入**（`knowledge_section`）——规则检索、agent 不驱动。升级：给执行器（claude code，有 bash）一个**知识查询 CLI**，它**自己决定何时查、查什么** → agentic 检索。

## 研究 grounding（上网查证）

- **机制常见**：给编码 agent（Claude Code/Cursor）加知识检索工具，主流是 **MCP server**（Weaviate/Qdrant/LanceDB MCP）。`kb.py` CLI = MCP 的轻量表亲。
- **知识域是我们的差异化**：绝大多数编码-agent RAG 检索**代码**（代码 embedding/AST 图）；我们检索**挖出来的 dev 痕迹**（bug/transcript/commit/bug-prone/cycle-time）——"项目开发史/失败模式"元知识喂编码 agent，少见、是卖点。
- **方向对**：编码 agent 对结构化数据偏好**确定性检索（graph/grep）**，向量只在 fuzzy 文档时赢。→ 两档：graph 精确（先）+ keyword 语义（先），向量留 phase 2。

## 设计（三层职责，不是"两档"）

纠正：**keyword 不做 semantic（反模式）**。三层职责分明：
- **agent（LLM）= semantic**：理解任务 → 决定查什么**精确 key**（哪个 task_type / 哪个文件 / 哪个 graph 节点）。语义在 agent 大脑。
- **kb.py = exact/确定性 fetcher**：graph 遍历、按 type/file 查、读 playbook。**keyword 只用于结构化精确匹配**（文件名、task_type——正当场景），**不试图做语义**。
- **embedding/向量 = fuzzy 兜底（phase 2）**：连 agent 都定不了精确 key 时才上，不是 keyword。

`kb.py`（`packages/story-miner/scripts/kb.py`），claude 用 bash 调：

| 子命令 | 数据源 | 干嘛 |
|---|---|---|
| `kb graph <node>` | `hc-all/.story/knowledge/graph/product-context-graph.json`（432 节点/718 边）| 遍历图：某 Service/Table/Feign 的调用方、所属域、读写表、MQ |
| `kb bugs <task_type\|file>` | `scripts/out/result_axis_phase2.json` + `bug_story_graph.json` | 该类/该文件的 bug-prone 文件、磁铁、cycle-time |
| `kb playbook <task_type>` | `hc-all/.story/knowledge/playbooks/{type}.md` | 该类的过程 playbook（高频文件/命令/坑）|

输出**简洁、token-conscious**（学 baoxian 的 token 预算，不灌全文）。

## Prompt 集成

执行器 prompt 从"预注入死包"改成"工具引导"：
```
## 项目知识（按需查，别全查）
CLI: `python kb.py <graph|bugs|playbook> <query>`（在 packages/story-miner/scripts/）
- 改某文件前 → `kb graph <file>` 看调用方/关联表/历史 bug
- 评估某类风险 → `kb bugs <task_type>`
- 想看怎么干 → `kb playbook <task_type>`
按需调用。
```
（首版可保留轻量预注入 teaser + 工具，跑通后再纯工具。）

## 升级路径

- **phase 2**：kb.py 包成 **MCP server**（Claude Code 原生、类型化、不 prompt hack）——同一查询逻辑换壳。
- **phase 3**：fuzzy 查询加 embedding/向量（baoxian 的 bottom-up），keyword 不够时补。

## 状态

- [ ] kb.py 三子命令（graph/bugs/playbook）
- [ ] 跑样例验证检索结果
- [ ] prompt 集成（设计/build/verify 加工具引导）
- [ ] （后）MCP 化
