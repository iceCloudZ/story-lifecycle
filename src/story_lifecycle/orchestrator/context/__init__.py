"""Context module — Resolver, Snapshot, Auto Discovery.

Provides AI sessions with stable, versioned context snapshots.
"""

from .resolver import ContextResolver, ContextBundle
from .snapshot import generate_snapshot
from .auto_discovery import Scanner, Decider, Handler, ScanResult, ContextMutation

__all__ = [
    "ContextResolver",
    "ContextBundle",
    "generate_snapshot",
    "Scanner",
    "Decider",
    "Handler",
    "ScanResult",
    "ContextMutation",
]
