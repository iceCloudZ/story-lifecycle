"""Learning sub-package — quality-flywheel seeding (seed_pipeline + seeds).

Stage-1 (ISS-010): seed_pipeline + seeds moved here from evaluation/ to break an
engine <-> evaluation cycle (seed_pipeline imports engine.planner to run a
seeding cycle; planner imports evaluation.gate). With seeding in its own
subpackage, the dependency is strictly learning -> engine -> evaluation
(one-way, no cycle).
"""
