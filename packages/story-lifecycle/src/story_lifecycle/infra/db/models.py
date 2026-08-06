"""SQLite data models — single-file, zero-ORM.

设计 15 阶段B：本文件曾是 3111 行的 god module，已拆分为
infra/db/ 子包（connection/schema/stories/sessions/... 18 个 family）。
本文件保留为门面，re-export 所有子模块符号，保证
``from ..infra.db import models as db; db.<fn>()`` 零改动可用。新代码请直接 import 子模块（如 ``from ..infra.db.stories import get_story``）。
"""

# ruff: noqa: F401  (门面 re-export——符号被外部 123+ 调用方使用,vulture/ruff 无法跨模块追踪)
from __future__ import annotations

# ---- connection ----
from .connection import get_db_path, get_conn, _db, _validate_columns, VALID_COLUMNS

# ---- schema ----
from .schema import (
    init_db,
    _create_story_tables,
    _create_session_tables,
    _create_finding_tables,
    _create_runtime_fact_tables,
    _create_doc_tables,
)
from .schema import (
    _create_change_item_tables,
    _create_delivery_tables,
    _create_trace_tables,
    _create_decision_tables,
    _backfill_llm_trace_story_keys,
)

# ---- stories ----
from .stories import (
    create_story,
    get_story,
    find_by_source_id,
    list_active_stories,
    list_candidate_stories,
    list_completed_stories,
)
from .stories import (
    list_visible_stories,
    get_sub_stories,
    list_stories_by_parent,
    list_unlinked_bugs,
    get_pending_parents,
    update_story,
)
from .stories import (
    update_context,
    log_stage,
    get_stage_logs,
    get_gate_results,
    delete_story,
    soft_delete_story,
)
from .stories import (
    restore_story,
    claim_story_driver,
    _driver_pid_alive,
    release_story_driver,
    upsert_story,
    upsert_story_from_source,
)
from .stories import COMPLETED_STATES

# ---- events ----
from .events import (
    log_event,
    record_gate_result,
    get_story_events,
    get_latest_declare,
    parse_event_payload,
    is_adversarial_loop_event,
)
from .events import get_recent_quality_events, get_recent_events_by_type

# ---- traces ----
from .traces import (
    log_llm_trace,
    log_llm_call,
    get_story_llm_calls,
    _pricing_for_model,
    get_story_token_usage,
    MODEL_PRICING_CNY,
)

# ---- findings ----
from .findings import (
    create_finding,
    get_finding,
    update_finding,
    get_open_findings,
    get_findings_by_status,
    get_all_pending_findings,
)
from .findings import (
    get_findings_by_story,
    get_finding_evidence,
    enrich_findings_with_evidence,
    SEVERITY_ORDER,
)

# ---- learned_patterns ----
from .learned_patterns import (
    create_learned_pattern,
    get_learned_pattern,
    update_learned_pattern,
    get_active_learned_patterns,
    get_proposed_learned_patterns,
    find_relevant_patterns,
)

# ---- context_revision ----
from .context_revision import get_context_revision, bump_context_revision

# ---- projects ----
from .projects import (
    create_project,
    get_project,
    get_project_by_name,
    list_projects,
    update_project,
    delete_project,
)

# ---- workspaces ----
from .workspaces import (
    create_workspace,
    get_workspace,
    get_workspace_by_slug,
    get_workspace_by_name,
    list_workspaces,
    update_workspace,
)
from .workspaces import (
    delete_workspace,
    update_workspace_init_state,
    list_projects_by_workspace,
    list_stories_by_workspace,
    WORKSPACE_INIT_STEPS,
)

# ---- story_project ----
from .story_project import (
    WorktreePathConflict,
    _find_worktree_occupant,
    _resolve_worktree_conflict,
    bind_story_project,
    get_story_project,
    get_story_projects,
)
from .story_project import (
    update_story_project,
    unbind_story_project,
    _DISPLACEABLE_STATES,
)

# ---- runtime_facts ----
from .runtime_facts import upsert_runtime_facts, get_runtime_facts

# ---- documents ----
from .documents import (
    _normalize_doc_ref,
    create_document,
    get_document,
    get_story_documents,
    update_document,
    delete_document,
)

# ---- change_items ----
from .change_items import (
    create_change_item,
    get_change_item,
    get_story_change_items,
    update_change_item,
)

# ---- deliveries ----
from .deliveries import (
    create_delivery_artifact,
    get_delivery_artifact,
    get_story_delivery_artifacts,
    update_delivery_artifact,
)

# ---- sessions ----
from .sessions import (
    compute_session_id,
    get_session,
    list_sessions_for_story,
    upsert_session,
    set_session_id,
    complete_session,
)
from .sessions import (
    set_session_completion_summary,
    delete_session,
    update_session_trace,
)

# ---- decisions ----
from .decisions import log_decision, get_decisions, count_decisions

# ---- story_docs ----
from .story_docs import (
    upsert_story_doc,
    set_story_doc_local_path,
    get_story_doc,
    confirm_story_doc,
    get_story_doc_version,
    list_story_doc_versions,
)
from .story_docs import list_story_docs, rollback_story_doc, search_docs
