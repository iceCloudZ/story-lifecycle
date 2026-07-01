"""Engine sub-package — the Function-Calling orchestration core.

Stage-1 layer partition (ISS-010): the FC core moved here from the orchestrator
root — planner (run_orchestrator_agent / continue_orchestrator_agent), agent_tools
(the FC tool definitions), graph (start_story_async / continue loop), stage_graph
/ graph_patch (stage topology + runtime patching), router (LLM routing), meta_planner
(scope/strategy/decomposition), policy_engine (guarded autonomy), shadow_router
(shadow proposals), execution (execution-mode parsing).

engine depends upward on db/adapters/terminal/llm_client and on the evaluation +
workspace + observability sub-packages (one-way); no sub-package imports back into
engine, so there is no cycle.
"""
