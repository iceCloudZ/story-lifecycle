"""⑤ Infrastructure layer — shared low-level concerns (config, db, llm client, prompts, terminal, benchmarks).

Physical layering (ISS-012, Cosmic Python style). This package is depended on by all
upper layers (entry / sourcing / orchestrator / knowledge) and must not import upward.
"""
