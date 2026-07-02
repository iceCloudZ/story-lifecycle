"""④ Knowledge layer — context providers, AI-CLI adapters, and the lifecycle knowledge store.

Physical layering (ISS-012, Cosmic Python style). Contains:
  context_providers/  — pluggable story-context injection (dynamically loads the miner
                        provider via importlib; never imports a concrete provider here)
  adapters/           — AI CLI adapters (Claude/Codex/Shell) behind a common BaseAdapter
  knowledge_store/    — the lifecycle-internal .story/knowledge read-write modules
                        (bootstrap/detector/scope/wizard/paths/...). Renamed from the
                        former top-level knowledge/ to disambiguate from this layer dir.

Depends downward on infra; nothing lower imports it.
"""
