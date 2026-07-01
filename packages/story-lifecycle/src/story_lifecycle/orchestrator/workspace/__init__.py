"""Workspace sub-package — project scanning/profiling/probing/registry,
resource locking, branch naming, doctor-paths checks, and worktree management.

Stage-1 layer partition (ISS-010): ``project_scan`` / ``project_profile`` /
``project_probe`` / ``project_registry`` / ``resource_lock`` / ``branch_naming``
/ ``doctor_paths`` moved here from the orchestrator root, and the existing
``worktree/`` sub-package nested under it as ``workspace.worktree``.

``paths.py`` deliberately stays at the orchestrator root — it is a cross-layer
shared utility (used by ④ knowledge, benchmarks, cli, orchestrator), so it is
infra-like and belongs at a shallow path (same pattern as ``config.py`` /
``json_helpers.py`` from ISS-006), not buried in a workspace subdir.
"""
