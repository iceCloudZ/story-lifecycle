"""Tests for Phase 6 modules: shadow_router, meta_planner,
stage_library, stage_graph, graph_patch, policy_engine (Guarded Apply).

The dual-flywheel sub-package (domain/engine/promotion) was removed in ISS-008
— dead code superseded by the live quality flywheel (db.models/seeds/quality).
"""

from story_lifecycle.orchestrator.engine.shadow_router import (
    ShadowDecision,
    ShadowTrigger,
    detect_triggers,
    generate_shadow_proposal,
    save_shadow,
    load_shadow,
    update_counterfactual,
    compute_shadow_stats,
)
from story_lifecycle.orchestrator.engine.meta_planner import (
    StoryScope,
    ExecutionMode,
    StrategyEnvelope,
    classify_scope,
    select_mode,
    generate_strategy,
    load_strategy,
    should_decompose,
    generate_task_packets,
)
from story_lifecycle.orchestrator.engine.stage_library import (
    StageCategory,
    BUILTIN_STAGES,
    get_stage_definition,
    is_valid_stage,
    validate_stage_inputs,
)
from story_lifecycle.orchestrator.engine.stage_graph import (
    build_default_graph,
    build_simple_graph,
    build_strict_graph,
    validate_graph,
)
from story_lifecycle.orchestrator.engine.graph_patch import (
    PatchType,
    PatchStatus,
    GraphPatch,
    assess_patch_risk,
    validate_patch,
    create_patch,
    approve_patch,
    apply_patch,
    reject_patch,
)
from story_lifecycle.orchestrator.engine.policy_engine import (
    AutonomyLevel,
    GuardedAutonomy,
    ActionCategory,
    GUARDED_RULES,
    DEFAULT_GUARDED_LEVEL,
    evaluate_guarded,
)


# ── Shadow Router tests ──


class TestShadowTriggerDetection:
    def test_no_triggers_on_happy_path(self):
        state = {
            "story_key": "T1",
            "review_summary": "",
            "last_error": "",
            "execution_count": 0,
        }
        triggers = detect_triggers(state, {})
        assert triggers == []

    def test_review_revise_trigger(self):
        state = {
            "story_key": "T2",
            "review_summary": "needs revise",
            "last_error": "",
            "execution_count": 0,
        }
        triggers = detect_triggers(state, {})
        assert ShadowTrigger.REVIEW_REVISE in triggers

    def test_low_trajectory_trigger(self):
        state = {
            "story_key": "T3",
            "review_summary": "",
            "last_error": "",
            "execution_count": 0,
            "trajectory_score": 0.2,
        }
        triggers = detect_triggers(state, {})
        assert ShadowTrigger.LOW_TRAJECTORY in triggers

    def test_review_fail_trigger(self):
        state = {
            "story_key": "T4",
            "review_summary": "complete fail",
            "last_error": "",
            "execution_count": 0,
        }
        triggers = detect_triggers(state, {})
        assert ShadowTrigger.REVIEW_FAIL in triggers


class TestShadowProposal:
    def test_no_proposal_without_triggers(self):
        state = {
            "story_key": "T1",
            "review_summary": "",
            "last_error": "",
            "execution_count": 0,
        }
        result = generate_shadow_proposal(state, {}, "advance", [])
        assert result is None

    def test_proposal_on_review_revise(self):
        state = {
            "story_key": "T2",
            "current_stage": "implement",
            "review_summary": "needs revise",
            "last_error": "revise needed",
            "execution_count": 2,
        }
        proposal = generate_shadow_proposal(
            state, {}, "retry", [ShadowTrigger.REVIEW_REVISE]
        )
        assert proposal is not None
        assert proposal.proposed_action == "switch_model"
        assert proposal.actual_action == "retry"
        assert proposal.confidence > 0

    def test_proposal_on_low_trajectory(self):
        state = {
            "story_key": "T3",
            "current_stage": "implement",
            "review_summary": "",
            "last_error": "",
            "execution_count": 0,
            "trajectory_score": 0.2,
        }
        proposal = generate_shadow_proposal(
            state, {}, "advance", [ShadowTrigger.LOW_TRAJECTORY]
        )
        assert proposal is not None
        assert proposal.proposed_action == "wait_confirm"


class TestShadowPersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.shadow_router.SHADOW_DIR", tmp_path
        )
        decision = ShadowDecision(
            shadow_id="abc123",
            story_key="T1",
            stage="implement",
            trigger=ShadowTrigger.LOW_TRAJECTORY,
            proposed_action="wait_confirm",
            proposed_detail="Pause for human",
            actual_action="advance",
            confidence=0.75,
            reason="Low trajectory",
            created_at="2026-01-01T00:00:00",
        )
        save_shadow(decision)
        loaded = load_shadow("abc123")
        assert loaded is not None
        assert loaded.shadow_id == "abc123"
        assert loaded.trigger == ShadowTrigger.LOW_TRAJECTORY

    def test_counterfactual_update(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.shadow_router.SHADOW_DIR", tmp_path
        )
        decision = ShadowDecision(
            shadow_id="def456",
            story_key="T2",
            stage="review",
            trigger=ShadowTrigger.REVIEW_REVISE,
            proposed_action="switch_model",
            proposed_detail="Switch",
            actual_action="retry",
            confidence=0.6,
            reason="Retry loop",
            created_at="2026-01-01T00:00:00",
        )
        save_shadow(decision)
        result = update_counterfactual(
            "def456", human_label="correct", later_outcome="story completed"
        )
        assert result is True
        loaded = load_shadow("def456")
        assert loaded.human_label == "correct"
        assert loaded.later_outcome == "story completed"

    def test_compute_stats(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.shadow_router.SHADOW_DIR", tmp_path
        )
        decision = ShadowDecision(
            shadow_id="stat1",
            story_key="T3",
            stage="design",
            trigger=ShadowTrigger.REVIEW_FAIL,
            proposed_action="fail",
            proposed_detail="Fail",
            actual_action="retry",
            confidence=0.8,
            reason="Review fail",
            created_at="2026-01-01T00:00:00",
            human_label="correct",
        )
        save_shadow(decision)
        stats = compute_shadow_stats()
        assert stats.total == 1
        assert stats.proposed_correct == 1


# ── Meta-Planner tests ──


class TestScopeClassification:
    def test_simple_scope(self):
        scope, signals = classify_scope(title="Fix typo", description="Simple fix")
        assert scope == StoryScope.SIMPLE

    def test_medium_scope(self):
        scope, signals = classify_scope(
            title="Add login page",
            description="Implement user login",
            acceptance_criteria=["User can log in", "Error handling"],
        )
        assert scope in (StoryScope.SIMPLE, StoryScope.MEDIUM)

    def test_epic_scope(self):
        scope, signals = classify_scope(
            title="重构系统架构迁移",
            description="Complete architecture refactor and migration",
            acceptance_criteria=[f"AC{i}" for i in range(10)],
            affected_modules=[f"mod{i}" for i in range(6)],
            prd_lines=200,
        )
        assert scope in (StoryScope.LARGE, StoryScope.EPIC)


class TestExecutionMode:
    def test_simple_scope_simple_mode(self):
        mode = select_mode(StoryScope.SIMPLE)
        assert mode == ExecutionMode.SIMPLE_PATH

    def test_epic_scope_decomposed(self):
        mode = select_mode(StoryScope.EPIC)
        assert mode == ExecutionMode.DECOMPOSED

    def test_strict_profile_override(self):
        mode = select_mode(StoryScope.SIMPLE, strict_profile=True)
        assert mode == ExecutionMode.STRICT


class TestStrategyEnvelope:
    def test_generate_and_load(self, tmp_path, monkeypatch, isolated_story_home):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.meta_planner.STRATEGY_DIR", tmp_path
        )

        envelope = generate_strategy(
            story_key="TEST-001",
            title="Add feature",
            description="Implement a new feature",
            acceptance_criteria=["Works", "Fast"],
        )
        assert envelope.story_key == "TEST-001"
        assert envelope.scope in (StoryScope.SIMPLE, StoryScope.MEDIUM)
        assert envelope.strategy_id != ""

        loaded = load_strategy("TEST-001")
        assert loaded is not None
        assert loaded.scope == envelope.scope


class TestDecomposition:
    def test_should_decompose_epic(self, tmp_path, monkeypatch, isolated_story_home):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.meta_planner.STRATEGY_DIR", tmp_path
        )
        envelope = StrategyEnvelope(
            strategy_id="test",
            story_key="TEST-002",
            scope=StoryScope.EPIC,
            mode=ExecutionMode.DECOMPOSED,
        )
        assert should_decompose(envelope) is True

    def test_should_not_decompose_simple(
        self, tmp_path, monkeypatch, isolated_story_home
    ):
        envelope = StrategyEnvelope(
            strategy_id="test",
            story_key="TEST-003",
            scope=StoryScope.SIMPLE,
            mode=ExecutionMode.SIMPLE_PATH,
        )
        assert should_decompose(envelope) is False

    def test_generate_task_packets(self, tmp_path, monkeypatch, isolated_story_home):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.meta_planner.STRATEGY_DIR", tmp_path
        )
        envelope = StrategyEnvelope(
            strategy_id="test",
            story_key="TEST-004",
            scope=StoryScope.EPIC,
            mode=ExecutionMode.DECOMPOSED,
            budget_minutes=60,
            budget_llm_calls=80,
        )
        subtasks = [
            {
                "key_suffix": "auth",
                "title": "Auth module",
                "summary": "Implement auth",
                "depends_on": [],
            },
            {
                "key_suffix": "api",
                "title": "API layer",
                "summary": "Implement API",
                "depends_on": ["auth"],
            },
        ]
        packets = generate_task_packets(envelope, subtasks)
        assert len(packets) == 2
        assert packets[0].key_suffix == "auth"
        assert packets[1].depends_on == ["auth"]
        assert packets[0].context_shard["budget_minutes"] == 30  # 60 / 2


# ── Stage Library tests ──


class TestStageLibrary:
    def test_builtin_stages_exist(self):
        assert "plan" in BUILTIN_STAGES
        assert "implement" in BUILTIN_STAGES
        assert "test" in BUILTIN_STAGES
        assert len(BUILTIN_STAGES) >= 9

    def test_get_stage_definition(self):
        stage = get_stage_definition("plan")
        assert stage is not None
        assert stage.category == StageCategory.PLANNING

    def test_is_valid_stage(self):
        assert is_valid_stage("plan") is True
        assert is_valid_stage("nonexistent") is False

    def test_validate_inputs(self):
        missing = validate_stage_inputs("plan", [])
        assert "prd" in missing
        assert "story_context" in missing

    def test_validate_inputs_satisfied(self):
        missing = validate_stage_inputs("plan", ["prd", "story_context"])
        assert missing == []


# ── Stage Graph tests ──


class TestStageGraph:
    def test_default_graph_valid(self):
        graph = build_default_graph()
        errors = validate_graph(graph)
        assert errors == []

    def test_simple_graph_valid(self):
        graph = build_simple_graph()
        errors = validate_graph(graph)
        assert errors == []

    def test_strict_graph_valid(self):
        graph = build_strict_graph()
        errors = validate_graph(graph)
        assert errors == []

    def test_valid_transition(self):
        graph = build_default_graph()
        assert graph.is_valid_transition("plan", "implement") is True
        assert graph.is_valid_transition("implement", "plan") is False

    def test_validate_path(self):
        graph = build_simple_graph()
        errors = graph.validate_path(["plan", "implement", "test"])
        assert errors == []

    def test_invalid_path(self):
        graph = build_simple_graph()
        errors = graph.validate_path(["implement", "plan", "test"])
        assert len(errors) > 0

    def test_find_all_paths(self):
        graph = build_simple_graph()
        paths = graph.find_all_paths("plan", "test")
        assert len(paths) >= 1
        assert ["plan", "implement", "test"] in paths


# ── Graph Patch tests ──


class TestGraphPatch:
    def test_assess_insert_risk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.graph_patch.PATCH_DIR", tmp_path
        )
        patch = GraphPatch(
            patch_id="p1",
            story_key="T1",
            patch_type=PatchType.INSERT_STAGE,
            target_stage="implement",
            insert_after="plan",
            new_stage="review",
        )
        risk = assess_patch_risk(patch)
        assert risk == "medium"

    def test_assess_deploy_insert_risk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.graph_patch.PATCH_DIR", tmp_path
        )
        patch = GraphPatch(
            patch_id="p2",
            story_key="T1",
            patch_type=PatchType.INSERT_STAGE,
            target_stage="final_review",
            insert_after="final_review",
            new_stage="deploy",
        )
        risk = assess_patch_risk(patch)
        assert risk == "critical"

    def test_create_and_approve(self, tmp_path, monkeypatch, isolated_story_home):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.graph_patch.PATCH_DIR", tmp_path
        )
        patch = create_patch(
            story_key="T1",
            patch_type=PatchType.PAUSE_FOR_HUMAN,
            target_stage="implement",
            reason="Uncertain outcome",
        )
        assert patch.status == PatchStatus.PROPOSED

        approved = approve_patch(patch.patch_id, validated_by="human")
        assert approved is not None
        assert approved.status == PatchStatus.VALIDATED

    def test_apply_patch(self, tmp_path, monkeypatch, isolated_story_home):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.graph_patch.PATCH_DIR", tmp_path
        )
        patch = create_patch(
            story_key="T1",
            patch_type=PatchType.SWITCH_MODEL,
            target_stage="implement",
            model_name="gpt-4",
            reason="Provider degradation",
        )
        approve_patch(patch.patch_id, validated_by="router")
        applied = apply_patch(patch.patch_id)
        assert applied is not None
        assert applied.status == PatchStatus.APPLIED

    def test_reject_patch(self, tmp_path, monkeypatch, isolated_story_home):
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.graph_patch.PATCH_DIR", tmp_path
        )
        patch = create_patch(
            story_key="T1",
            patch_type=PatchType.SKIP_STAGE,
            target_stage="review",
            reason="Skip review",
        )
        rejected = reject_patch(patch.patch_id, reason="Too risky")
        assert rejected is not None
        assert rejected.status == PatchStatus.REJECTED

    def test_validate_insert_stage(self):
        patch = GraphPatch(
            patch_id="v1",
            story_key="T1",
            patch_type=PatchType.INSERT_STAGE,
            target_stage="implement",
            insert_after="plan",
            new_stage="review",
        )
        errors = validate_patch(patch)
        # May have "requires human approval" error for medium risk
        assert all("requires human approval" in e or e == "" for e in errors)

    def test_validate_missing_new_stage(self):
        patch = GraphPatch(
            patch_id="v2",
            story_key="T1",
            patch_type=PatchType.INSERT_STAGE,
            target_stage="implement",
        )
        errors = validate_patch(patch)
        assert any("new_stage" in e for e in errors)


# ── Guarded Apply (Policy Engine) tests ──


class TestGuardedApply:
    def test_l0_all_confirm(self):
        result = evaluate_guarded(
            action="retry",
            action_category=ActionCategory.ROUTING,
            story_key="T1",
            autonomy_level=GuardedAutonomy.L0_FULL_MANUAL,
        )
        assert result.decision == AutonomyLevel.CONFIRM

    def test_l1_shadow_only(self):
        result = evaluate_guarded(
            action="retry",
            action_category=ActionCategory.ROUTING,
            story_key="T2",
            autonomy_level=GuardedAutonomy.L1_SHADOW_ONLY,
        )
        assert result.decision == AutonomyLevel.SHADOW

    def test_l3_auto_routing(self):
        result = evaluate_guarded(
            action="advance",
            action_category=ActionCategory.ROUTING,
            story_key="T3",
            autonomy_level=GuardedAutonomy.L3_SUPERVISED,
        )
        assert result.decision == AutonomyLevel.APPLY

    def test_l3_confirm_model_switch(self):
        result = evaluate_guarded(
            action="switch_to_gpt4",
            action_category=ActionCategory.MODEL_SWITCH,
            story_key="T4",
            autonomy_level=GuardedAutonomy.L3_SUPERVISED,
        )
        assert result.decision == AutonomyLevel.CONFIRM

    def test_l4_auto_model_switch(self):
        result = evaluate_guarded(
            action="switch_to_gpt4",
            action_category=ActionCategory.MODEL_SWITCH,
            story_key="T5",
            autonomy_level=GuardedAutonomy.L4_AUTONOMOUS,
        )
        assert result.decision == AutonomyLevel.APPLY

    def test_destructive_always_restricted(self):
        result = evaluate_guarded(
            action="delete_all",
            action_category=ActionCategory.DESTRUCTIVE,
            story_key="T6",
            autonomy_level=GuardedAutonomy.L4_AUTONOMOUS,
        )
        assert result.decision in (AutonomyLevel.CONFIRM, AutonomyLevel.FORBIDDEN)

    def test_l5_destructive_needs_confirm(self):
        result = evaluate_guarded(
            action="delete_all",
            action_category=ActionCategory.DESTRUCTIVE,
            story_key="T7",
            autonomy_level=GuardedAutonomy.L5_FULL_AUTO,
        )
        assert result.decision == AutonomyLevel.CONFIRM

    def test_budget_exhaust_downgrade(self):
        result = evaluate_guarded(
            action="retry",
            action_category=ActionCategory.ROUTING,
            story_key="T8",
            autonomy_level=GuardedAutonomy.L4_AUTONOMOUS,
            budget_remaining={"minutes": 0, "llm_calls": 0},
        )
        assert result.decision == AutonomyLevel.CONFIRM
        assert result.budget_check_passed is False

    def test_read_only_always_apply(self):
        for level in GuardedAutonomy:
            result = evaluate_guarded(
                action="query_state",
                action_category=ActionCategory.READ_ONLY,
                story_key="T9",
                autonomy_level=level,
            )
            assert result.decision == AutonomyLevel.APPLY, (
                f"Read-only should be APPLY at {level.value}"
            )

    def test_default_level_is_l2(self):
        assert DEFAULT_GUARDED_LEVEL == GuardedAutonomy.L2_CONFIRM

    def test_rules_matrix_completeness(self):
        """Every (level, category) combination should have a rule."""
        for level in GuardedAutonomy:
            for category in ActionCategory:
                key = (level.value, category.value)
                assert key in GUARDED_RULES, f"Missing rule for {key}"
