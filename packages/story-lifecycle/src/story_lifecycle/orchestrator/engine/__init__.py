"""Engine sub-package — the Function-Calling orchestration core.

planner (run_orchestrator_agent / continue_orchestrator_agent), graph
(start_story_async / continue loop), router (LLM routing), execution
(execution-mode parsing), supervisor (PTY 卡住检测/escalate), consult_runner /
consult_orchestrator (DESIGN §8.3 consult 子流程)。

engine depends upward on db/adapters/terminal/llm_client and on the evaluation +
workspace + observability sub-packages (one-way); no sub-package imports back
into engine, so there is no cycle.
"""
