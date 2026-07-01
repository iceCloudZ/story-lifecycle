"""Service sub-package — the story / API service layer.

Stage-1 layer partition (ISS-010): ``api`` (FastAPI app + endpoints),
``sync_service`` (TAPD sync), and ``service`` (story lifecycle ops, renamed to
``story_service`` to avoid a ``service.service`` path collision) moved here from
the orchestrator root.

``entry.py`` deliberately stays at the orchestrator root: it is shared by both
``service.api`` and ``observability.debug_packet`` (CliExitState). Moving it into
``service/`` would create a service <-> observability cycle (api imports
``observability.events.build_debug_response``; debug_packet imports ``entry``).
"""
