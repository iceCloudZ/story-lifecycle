"""Workspace sub-package — project scanning/profiling/probing/registry,
branch naming, doctor-paths checks, and worktree management.

``project_scan`` / ``project_profile`` / ``project_probe`` / ``project_registry``
/ ``branch_naming`` / ``doctor_paths`` 在此子包,``worktree/`` 嵌套为
``workspace.worktree``。

``paths.py`` deliberately stays at the orchestrator root — it is a cross-layer
shared utility (used by ④ knowledge, benchmarks, cli, orchestrator), so it is
infra-like and belongs at a shallow path (same pattern as ``config.py`` /
``json_helpers.py`` from ISS-006), not buried in a workspace subdir.
"""
