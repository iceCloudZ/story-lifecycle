# Story 长期资料与 TAPD 集成 — 全量实施计划

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans.
> Steps use checkbox (- [ ]) for tracking.

**Goal:** Story 长期结构化资料、多仓库 Worktree 隔离、Session 快照注入、代码交付闭环、TAPD 边界止血。

**Architecture:** Resolver-Decider-Handler 三层。context_json 降级为运行时瞬态。6 张新表承载长期事实。

**Spec:** `docs/superpowers/specs/2026-06-11-story-context-and-tapd-lifecycle-design.md`

---

## Task 1: 数据模型 — 6 张新表 + story 扩展

**Files:** Modify `src/story_lifecycle/db/models.py`

- [ ] Step 1: VALID_COLUMNS 加 `intake_state`, `context_revision`
- [ ] Step 2: init_db ALTER TABLE 加 intake_state TEXT DEFAULT 'ready', context_revision INTEGER DEFAULT 0
- [ ] Step 3: CREATE TABLE project (id, name UNIQUE, repo_path UNIQUE, default_branch, remote_url, availability, availability_reason, timestamps)
- [ ] Step 4: CREATE TABLE story_project (id, story_key FK CASCADE, project_id FK CASCADE, branch, base_branch, base_commit, worktree_path UNIQUE, workspace_type, worktree_state, summary, source, evidence_ref, timestamps, UNIQUE(story_key,project_id))
- [ ] Step 5: CREATE TABLE project_runtime_fact (id, project_id FK CASCADE, runtime_type, runtime_version, dependency_ref, check_command, availability, evidence_ref, updated_at)
- [ ] Step 6: CREATE TABLE story_document (id, story_key FK CASCADE, project_id FK SET NULL, kind, ref, summary, source, evidence_ref, verification_state, timestamps)
- [ ] Step 7: CREATE TABLE story_change_item (id, story_key FK CASCADE, project_id FK SET NULL, kind, ref, summary, lifecycle_state, verification_state, environment, source, evidence_ref, timestamps)
- [ ] Step 8: CREATE TABLE story_delivery_artifact (id, story_key FK CASCADE, project_id FK SET NULL, kind, provider, external_id, url, source_branch, target_branch, delivery_state, review_state, merge_commit, review_summary, source, evidence_ref, timestamps)
- [ ] Step 9: 写 CRUD 函数: create/get/list/update/delete_project, bind/get/update_story_project, upsert/get_runtime_facts, create/get/update/delete_document, create/get/update_change_item, create/get/update_delivery_artifact, bump/get_context_revision
- [ ] Step 10: 更新 upsert_story_from_source — 新增参数 intake_state="candidate", status="idle"
- [ ] Step 11: python -c "from story_lifecycle.db.models import init_db; init_db()" 验证建表
- [ ] Step 12: Commit

## Task 2: 边界止血 — intake_state 守卫

**Files:** Modify `sync_service.py`, `graph.py`, `graph_nodes.py`; Create `tests/test_intake_boundary.py`

- [ ] Step 1: sync_service.py — upsert_story_from_source 调用加 intake_state="candidate", status="idle"
- [ ] Step 2: models.py list_active_stories — WHERE 加 intake_state='ready'
- [ ] Step 3: graph.py recover_orphan_stories — 加 story.get("intake_state") == "ready" 守卫
- [ ] Step 4: graph.py start_story_async — 开头加 candidate 拒绝 (return early)
- [ ] Step 5: graph_nodes.py advance_node — 删除 source.sync_status("completed") 自动 TAPD 更新代码块
- [ ] Step 6: 写 tests/test_intake_boundary.py — 4 个测试:
  - test_sync_creates_candidate_idle
  - test_start_story_async_rejects_candidate
  - test_list_active_stories_excludes_candidates
  - test_recover_orphan_skips_candidates
- [ ] Step 7: pytest tests/test_intake_boundary.py -v (4 PASS)
- [ ] Step 8: Commit

## Task 3: 项目注册表 + 运行时事实

**Files:** Create `src/story_lifecycle/orchestrator/project_registry.py`; Create `tests/test_project_registry.py`

- [ ] Step 1: project_registry.py — register_project(name, repo_path, ...) 调用 db.create_project, 检查 repo_path: Path.resolve() 规范化, 路径不存在时设 availability="missing"
- [ ] Step 2: project_registry.py — check_project_availability(project_id) 检查路径存在 + git rev-parse --is-inside-work-tree, 更新 availability 字段
- [ ] Step 3: project_registry.py — list_projects(), get_project(id), update_project(id, **kw)
- [ ] Step 4: project_registry.py — add_runtime_fact(project_id, runtime_type, ...) 调用 db.upsert_runtime_fact
- [ ] Step 5: 写 tests/test_project_registry.py — 5 个测试:
  - test_register_project_normalizes_path
  - test_register_project_missing_path_sets_availability
  - test_check_availability_valid_git_repo
  - test_check_availability_not_git
  - test_add_runtime_fact
- [ ] Step 6: pytest tests/test_project_registry.py -v (5 PASS)
- [ ] Step 7: Commit

## Task 4: Worktree — Resolver + Decider + Handler

**Files:** Create `src/story_lifecycle/orchestrator/worktree/__init__.py`, `resolver.py`, `decider.py`, `handler.py`; Create `tests/test_worktree.py`

- [ ] Step 1: worktree/resolver.py — resolve_worktrees(project_path) 执行 git worktree list --porcelain -z, 解析输出为 dict{path: {branch, HEAD, locked}}
- [ ] Step 2: worktree/resolver.py — resolve_story_worktree(story_key) 读取 story_project + resolve_worktrees, 输出 WorktreeState: unprepared|available|missing|stale|conflict|unknown
- [ ] Step 3: worktree/decider.py — decide_prepare(story_project, worktree_map) 纯函数, 实现决策表:
  - worktree 不存在, 分支不存在 → create
  - worktree 存在, 分支匹配, 未被占用 → reuse
  - 路径存在但不是 git worktree → reject (path_conflict)
  - 分支已被其他 worktree checkout → reject (branch_checked_out_elsewhere)
  - worktree 分支不匹配 → reject (stale)
- [ ] Step 4: worktree/decider.py — decide_cleanup(story_project, delivery_state) 纯函数:
  - delivery_state in (merged, abandoned) → allow_cleanup
  - 其他 → reject (delivery_not_finalized)
  - worktree 非 clean → reject (worktree_dirty)
- [ ] Step 5: worktree/handler.py — prepare_worktrees(story_key) 遍历 story_projects, 调 decider, 执行 git worktree add / git branch, 更新 worktree_path + worktree_state
- [ ] Step 6: worktree/handler.py — cleanup_worktree(story_key, project_id) 需用户确认, git worktree remove, P0 不删分支
- [ ] Step 7: 写 tests/test_worktree.py — 使用 tmp_path 创建临时 git 仓库:
  - test_resolve_empty_worktrees
  - test_two_stories_isolated_worktrees
  - test_branch_conflict_rejected
  - test_path_conflict_rejected
  - test_stale_branch_rejected
  - test_dirty_worktree_no_cleanup
  - test_delivery_not_finalized_no_cleanup
- [ ] Step 8: pytest tests/test_worktree.py -v (7 PASS)
- [ ] Step 9: Commit

## Task 5: 交付产物 — CRUD + 状态机 + 清理门禁

**Files:** Create `src/story_lifecycle/orchestrator/delivery.py`; Create `tests/test_delivery.py`

- [ ] Step 1: delivery.py — register_delivery(story_key, kind, ...) 调用 db.create_delivery_artifact, kind=local_merge 时必须非空 merge_commit + evidence_ref
- [ ] Step 2: delivery.py — update_delivery_state(artifact_id, new_state, source) AI 禁止设 abandoned; source="ai" + new_state="abandoned" 抛出 PermissionError
- [ ] Step 3: delivery.py — record_review(artifact_id, review_state, summary) 更新 review_state + review_summary
- [ ] Step 4: delivery.py — can_cleanup_worktree(story_key) 检查所有 delivery_artifact: 全部 merged 或 abandoned 才允许
- [ ] Step 5: 写 tests/test_delivery.py — 6 个测试:
  - test_register_github_pr
  - test_local_merge_requires_merge_commit
  - test_ai_cannot_abandon_delivery
  - test_user_can_abandon_delivery
  - test_review_recording
  - test_cleanup_gate_blocks_unmerged
- [ ] Step 6: pytest tests/test_delivery.py -v (6 PASS)
- [ ] Step 7: Commit

## Task 6: Context Resolver + Snapshot + 自动发现

**Files:** Create `context/__init__.py`, `resolver.py`, `snapshot.py`, `auto_discovery.py`; Create `tests/test_context.py`, `tests/test_auto_discovery.py`

- [ ] Step 1: context/resolver.py — ContextResolver.resolve(story_key) 读取 story + story_projects + documents + change_items + delivery_artifacts + runtime_facts, 校验: 路径存在, 文件可读, URL 格式, Profile/Stage 合法, 状态值合法. 输出 ContextBundle dataclass
- [ ] Step 2: context/resolver.py — validate_context_bundle(bundle) 校验逻辑, 输出 errors list
- [ ] Step 3: context/snapshot.py — generate_snapshot(story_key) 调 Resolver, 渲染 Markdown 到 .story/context/{story_key}/story-context-r{revision}.md, 记录 event_log context_snapshot_created
- [ ] Step 4: context/snapshot.py — Snapshot 内容: Story 标识+标题+阶段, Profile+Stage+Goal+Expected Outputs+Quality Gates, TAPD 镜像字段+链接, 所有项目+Worktree+分支+基线, 运行时事实+检查结果, PRD/设计文档引用+摘要, DDL/Nacos 状态+可信度+证据, 交付产物+审查状态+合并证据, context_revision
- [ ] Step 5: context/auto_discovery.py — Scanner.scan(story_key) 读取 worktree (worktree_state=unprepared 降级读 repo_path), git diff base_commit..HEAD, find SQL/config/doc files, 输出 ScanResult
- [ ] Step 6: context/auto_discovery.py — Decider.merge(current_facts, scan_result) 纯函数比较, 输出 ContextMutation (新增/更新/矛盾/忽略)
- [ ] Step 7: context/auto_discovery.py — Handler.apply(story_key, mutations) 短事务: apply mutations, log events, bump context_revision
- [ ] Step 8: 写 tests/test_context.py — 6 个测试:
  - test_resolver_reads_all_entities
  - test_resolver_flags_missing_path
  - test_resolver_flags_invalid_profile_stage
  - test_snapshot_contains_all_sections
  - test_snapshot_records_revision
  - test_optimistic_lock_conflict_409
- [ ] Step 9: 写 tests/test_auto_discovery.py — 5 个测试:
  - test_scanner_finds_sql_files
  - test_scanner_finds_nacos_refs
  - test_scanner_no_worktree_falls_back_to_repo
  - test_decider_merge_detects_new_facts
  - test_handler_applies_and_bumps_revision
- [ ] Step 10: pytest tests/test_context.py tests/test_auto_discovery.py -v (11 PASS)
- [ ] Step 11: Commit

## Task 7: API 端点 + Prompt 注入

**Files:** Modify `api.py`, `prompt_renderer.py`, `graph_nodes.py`, `state.py`

- [ ] Step 1: api.py — GET /api/story/{key}/context 返回 ContextBundle JSON (projects, documents, change_items, delivery_artifacts, runtime_facts, revision)
- [ ] Step 2: api.py — PUT /api/story/{key}/context 接收 {"revision": N, "projects": [...], "documents": [...], "change_items": [...]}, 校验 revision, 冲突返回 409 {"ok":false, "reasonCode":"context_revision_conflict", "current_revision": M}
- [ ] Step 3: api.py — POST /api/story/{key}/context/refresh 触发单 Story auto_discovery, 不启动 AI
- [ ] Step 4: api.py — GET /api/story/{key}/context/snapshot 返回最新快照内容
- [ ] Step 5: api.py — GET/POST /api/projects, PUT /api/projects/{id}
- [ ] Step 6: api.py — POST /api/story/{key}/worktrees/prepare, GET cleanup-preview, POST cleanup (需用户确认)
- [ ] Step 7: api.py — GET/POST /api/story/{key}/delivery-artifacts, PUT /api/story/{key}/delivery-artifacts/{id}
- [ ] Step 8: api.py — POST /api/story/{key}/start (校验 project 绑定: tapd+candidate+无项目→拒绝; 手工+无项目→允许)
- [ ] Step 9: api.py — GET /api/story/{key}/tapd-writeback-suggestion (P0: 只读建议, 不执行)
- [ ] Step 10: graph_nodes.py plan_stage_node — 在 plan 文件写入前调 snapshot.generate_snapshot(), 将路径注入 state["_context_snapshot_path"]
- [ ] Step 11: prompt_renderer.py — 新增 {context_snapshot_section} 变量, 读取快照内容注入 stage prompt
- [ ] Step 12: state.py — StoryState 增加 _context_snapshot_path: Optional[str]
- [ ] Step 13: 写 tests/test_api_context.py — 8 个测试:
  - test_get_context
  - test_put_context_revision_conflict
  - test_refresh_context_no_ai_launch
  - test_get_snapshot
  - test_project_crud
  - test_prepare_worktrees
  - test_delivery_crud
  - test_candidate_start_rejected_no_project
- [ ] Step 14: pytest tests/test_api_context.py -v (8 PASS)
- [ ] Step 15: Commit

## Task 8: 全量回归测试

**Files:** 无新建, 运行全部测试

- [ ] Step 1: pytest tests/ -v 运行全部测试
- [ ] Step 2: 修复任何回归失败
- [ ] Step 3: ruff check src/ tests/ 无新增 lint 错误
- [ ] Step 4: git diff --stat main 确认变更范围合理
- [ ] Step 5: Commit

---

## 实施顺序

```
Task 1 (数据模型) → Task 2 (边界止血)
                 → Task 3 (项目注册表)
                 → Task 4 (Worktree) → Task 5 (交付产物)
                 → Task 6 (Context + 自动发现)
                 → Task 7 (API + Prompt) → Task 8 (回归)
```

Tasks 2 和 3 可并行。Task 7 依赖 Task 3/4/5/6 完成。
