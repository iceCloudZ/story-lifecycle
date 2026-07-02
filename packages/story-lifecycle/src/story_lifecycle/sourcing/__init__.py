"""② Sourcing layer — story intake & project planning (sources / planner / integrations).

Physical layering (ISS-012, Cosmic Python style). sources/ fetches stories from
TAPD/GitHub/etc., planner/ does project-level roadmap decomposition (NOT the stage
orchestrator planner, which lives at orchestrator/engine/planner.py), integrations/
holds upstream adapters (gitlab). Depends downward on infra; nothing lower imports it.
"""
