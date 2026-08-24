# DESIGN · story 文档托管服务器化 — DocStore seam + ys-agent 集成（思路稿）

> 状态：**思路记录，未立项**。2026-08-24 一次集成评估的产物，后面再优化。
> 评估方式：两边代码全量摸底（story-lifecycle 文档链路 + D:/java-agent 五个子项目的存储/认证/版本能力）。
> 本文固化评估事实与分阶段思路，防止散失；真做时从「阶段 0」起步并把本文升格为正式设计。

## 1. 目标

把 story 文档管理（`story_doc` 版本化存储 + 本地 `.md` 缓存 + 知识库）从「单机 SQLite + 本地文件」演进为可托管到服务器，与 `D:/java-agent`（企业 AI Agent 平台 monorepo，核心是 ys-agent）集成。**不是**把编排器整体上云（那是平台化长线），只动文档层。

## 2. 评估事实（两边现状）

### 2.1 story-lifecycle 侧：文档三处存储，无 DocStore 抽象

| 存储 | 位置 | 角色 |
|---|---|---|
| SQLite | `~/.story-lifecycle/story.db`：`story_doc`（最新内容）/`story_doc_version`（版本）/`story_doc_fts`（FTS5） | 声明的真相源（`infra/db/story_docs.py`） |
| 本地文件 | story evidence 目录 `<repo根>/story/<id>-<slug>/PRD.md·spec.md·test-report.md` + `.meta`（version+sha256） | 只读缓存，`doc_sync.sync_doc_to_local` 是唯一写缓存点 |
| 知识库 | `.story/knowledge/`（playbooks/scenarios/wiki/failures + INDEX.json） | 飞轮读写；`resolve_knowledge_root()`（知识飞轮 B4 修复引入）已支持 config/env 覆盖根路径 |

关键事实：

- **两个咽喉点**。所有写入经 `declare_artifact()`（`orchestrator/engine/artifact_declare.py`：原子写文件 + `upsert_story_doc` + done 兼容视图 + `artifact_declared` 事件，同批尽力落）；DB↔本地缓存全部经 `infra/doc_sync.py`。远程层只需接这两处。
- **没有 DocStore 协议**——持久化硬绑 SQLite + 本地 FS。但插件先例成熟可抄：`context_provider`（config.yaml + importlib 加载）、`StorySource`（TAPD/GitHub 远程源）、`BaseAdapter`（最干净的 Definition/Provider/Consumer 边界）。
- 完成判定读本地：`check_artifacts_landed`（`orchestrator/engine/artifact_check.py`）查本地文件存在性，`get_latest_declare` 查本地 SQLite `event_log`。

### 2.2 java-agent 侧：ys-agent 是唯一合适宿主

| 项目 | 判定 |
|---|---|
| **ys-agent** | ✅ 唯一宿主。生产级设施齐全（见下） |
| datahub | ❌ 纯 ODPS 表权限闸门，无文档能力 |
| ai-gateway | ❌ 纯 LLM 网关 |
| hc-auth-center | ❌ 只有 RBAC（可作将来统一认证参考） |
| aiops-mcp | ❌ 有 Python `ArtifactStore`+OSS 发布但无对外摄入/认证/元数据库，仅参考 |

ys-agent 已有地基（全部生产级）：

- `OssService`（Aliyun OSS 上传/流式下载/预签名 URL），`agent-app/.../service/OssService.java`
- `t_version_history`：**通用 git 式版本表**，按 `(target_type, target_id)` 键、JSONB 快照、`current` 部分唯一索引——已为 SKILL 复用过，STORY_DOC 直接映射（`target_id = {story_key}:{doc_type}`）
- 两张**建好未用**的表：`t_file_asset`（通用文件元数据，OSS 后端）、`t_artifact`（storage_backend local/oss/minio + sha256 + pinning）——实体和 Mapper 都在，无 Controller
- JWT 认证（`POST /api/auth/login` → Bearer）+ 审计（`t_audit_log`）
- 注意坑：MCP 端 `McpAuthFilter` 接受任意非空 token（demo 级），外部集成别走它；migration 必须按序续 v20+；生产部署 103（后端）/107（前端）是两个 Skyladder project。

## 3. 核心约束：本地优先，尽力推送

**阶段完成判定不能依赖网络。** artifact-driven 循环（`check_artifacts_landed` 读本地文件、`get_latest_declare` 读本地 SQLite、worktree 里的代码 agent 写本地文件）是编排器的命脉。正确架构是**本地优先 + 服务器异步同步**（类 git 的 centralized-but-local 模型）：

- `declare_artifact` 保持本地原子落库（现状不变）；
- 服务器推送是 best-effort 软接缝（失败重试/补推，不阻塞 stage 推进）；
- 远程是汇聚视图（跨机浏览/权限/审计），不是写入路径的必经点。

另注意：**文档上服务器 ≠ 多机协作**。编排状态仍在单机 SQLite；若目标是多机跑 story，DB 同步是另一个更大的问题（见平台化长线）。

## 4. 分阶段方案

- **阶段 0（本仓库，前置，零风险）**：立 `DocStore` 协议（Definition：`upsert/get/list_versions/rollback/search`），写 `LocalDocStore` 封装现有 `infra/db/story_docs.py` 行为；`declare_artifact` 与 `doc_sync` 改为经协议调用。纯重构，全量 pytest 验证。
- **阶段 1（ys-agent 侧）**：agent-app 新增 `StoryDocController`（如 `/api/v1/story-docs`）：内容走 `OssService`，版本走 `t_version_history`（`target_type='STORY_DOC'`），对象元数据启用 `t_file_asset`。migration v20+。认证：服务账号 JWT，或抄 datahub 的 `X-Service-Token` 内部过滤器模式。
- **阶段 2（接线）**：story-lifecycle 实现 `RemoteDocStore`（httpx + JWT），config.yaml 插件式加载（照 `context_provider` 加载器先例：`doc_store: {module, class, base_url, ...}`）。declare 后异步推送，版本号两边都是整数递增快照，直接映射。

## 5. 待拍板

1. **托管范围**：只 story 文档（走 DocStore seam），还是连知识库（`.story/knowledge/`）一起？知识库已有更便宜的先手——`resolve_knowledge_root` 支持 config 指向共享/同步目录，可先实现「多机共享一个知识库」不必上服务器。
2. ys-agent 侧 migration/部署节奏需要对方配合。

## 6. 扩展方向（同源集成还能做什么）

按「复用 ys-agent 现成设施」的思路，同一批地基还能撬动的事，按价值/成本粗排：

1. **LLM 出口统一到 ai-gateway**（低成本高价值）：story-lifecycle 的 `infra/llm_client.py` 是 OpenAI 兼容 httpx，ai-gateway 恰好是 OpenAI 兼容网关（含 Anthropic/Gemini 协议转换、客户端配额/路由管理）。把 LLM base_url 指过去即得统一模型路由、配额与成本观测——可能只需改 config，零代码。
2. **playbook → ys-agent 技能商店**（飞轮出口）：`reflection` 产出的 playbooks（`<knowledge_root>/playbooks/<task_type>/<dimension>.md`）打包 zip 推到 `SkillController POST /api/v1/skills`（本就接受 multipart zip 上传 + 版本记录）——让数字员工平台消费 story-lifecycle 沉淀的项目经验，知识飞轮第一次跨系统闭环。
3. **story-lifecycle 注册为 ys-agent 的 MCP server**：ys-agent 有现成自注册端点 `POST /internal/mcp/register`（ingress 过滤、免用户 JWT），把「查 story 进度/读 story 文档」暴露成 `ys.*` 工具，数字员工对话里可直接问「我那个需求跑到哪了」。
4. **知识库多机共享**（半成品）：`resolve_knowledge_root` 的 config 覆盖 + 全局默认（`D:/hc-all/.story/knowledge`）已把路铺好，配一个同步盘/网盘目录即实现多机共享知识库，先不上服务器。
5. **交付物托管**：`story_delivery_artifact` 表的交付件（dist 包等）推 OSS（复用 ys-agent `OssService` 或 aiops-mcp 的 `storage.publish` 思路），解决「交付物躺在 worktree 里」的问题。
6. **统一审计**：`orchestrator_decision` 决策事件汇到 ys-agent `t_audit_log`，编排行为进企业审计视图。
7. **平台化长线**（已有独立 idea）：多租户执行隔离 / SQLite→Postgres / Temporal 调度——本文档的 DocStore seam 是其中文档层的一块砖。

## 7. 关联

- LifeOS idea：`工作/个人项目/story-docstore-remote-hosting.md`（生命周期状态在此跟踪）
- 上位思路：「story-lifecycle 平台化：单机编排器→多租户在线平台」（2026-07-23）
- 知识飞轮 B4 修复（`resolve_knowledge_root` 单一解析入口）是本文托管范围决策的依赖项之一
