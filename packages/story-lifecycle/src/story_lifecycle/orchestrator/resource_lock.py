"""Resource Lock — define four lock types for parallel story execution.

Lock types:
- file_glob: specific files or glob patterns
- domain_area: logical domain (e.g. "auth", "payment")
- db_table: database tables
- api_prefix: API route prefixes

Decomposition Plan extensions add resource_locks to each task.
Dry-run scheduler simulates parallel execution and outputs conflict report.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ResourceLock:
    """A lock on a specific resource type."""

    lock_type: str = ""  # file_glob, domain_area, db_table, api_prefix
    pattern: str = ""  # the glob, domain name, table name, or route prefix
    mode: str = "exclusive"  # exclusive or shared
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictReport:
    """Report of conflicts detected during dry-run scheduling."""

    conflicts: list[dict[str, Any]] = field(default_factory=list)
    safe_parallelism: int = 1
    total_tasks: int = 0


def detect_conflicts(
    tasks: list[dict[str, Any]],
) -> ConflictReport:
    """Detect resource lock conflicts across tasks.

    Each task should have a 'resource_locks' field containing ResourceLock dicts.
    """
    report = ConflictReport(total_tasks=len(tasks))
    conflicts = []

    # Build lock index: {lock_type+pattern: [task_indices]}
    lock_index: dict[str, list[int]] = {}
    for i, task in enumerate(tasks):
        locks = task.get("resource_locks", [])
        for lock_dict in locks:
            key = f"{lock_dict.get('lock_type', '')}:{lock_dict.get('pattern', '')}"
            if key not in lock_index:
                lock_index[key] = []
            lock_index[key].append(i)

    # Find exclusive lock conflicts
    for key, indices in lock_index.items():
        if len(indices) > 1:
            # Check if any are exclusive
            for i in indices:
                locks = tasks[i].get("resource_locks", [])
                for lock_dict in locks:
                    lock_key = f"{lock_dict.get('lock_type', '')}:{lock_dict.get('pattern', '')}"
                    if (
                        lock_key == key
                        and lock_dict.get("mode", "exclusive") == "exclusive"
                    ):
                        # This task has exclusive lock — conflict with all others
                        for j in indices:
                            if j != i:
                                conflicts.append(
                                    {
                                        "resource": key,
                                        "task_a": tasks[i].get(
                                            "story_key", f"task-{i}"
                                        ),
                                        "task_b": tasks[j].get(
                                            "story_key", f"task-{j}"
                                        ),
                                        "reason": f"Exclusive lock conflict on {key}",
                                    }
                                )

    report.conflicts = conflicts
    report.safe_parallelism = len(tasks) - len(conflicts) if len(tasks) > 0 else 1
    return report


def dry_run_schedule(
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate parallel execution and output scheduling report.

    Returns a report with:
    - conflict_report: ConflictReport
    - schedule: proposed task ordering
    - parallel_groups: groups that can run concurrently
    """
    conflict_report = detect_conflicts(tasks)

    # Build parallel groups using conflict info
    conflict_pairs = set()
    for c in conflict_report.conflicts:
        a = c["task_a"]
        b = c["task_b"]
        conflict_pairs.add((a, b))
        conflict_pairs.add((b, a))

    # Simple greedy grouping
    groups: list[list[str]] = []
    remaining = [t.get("story_key", f"task-{i}") for i, t in enumerate(tasks)]

    while remaining:
        group = []
        for key in remaining:
            # Check if key conflicts with any in current group
            has_conflict = any((key, g) in conflict_pairs for g in group)
            if not has_conflict:
                group.append(key)
        groups.append(group)
        remaining = [k for k in remaining if k not in group]

    return {
        "conflict_report": asdict(conflict_report),
        "parallel_groups": groups,
        "total_tasks": len(tasks),
        "estimated_parallelism": max(len(g) for g in groups) if groups else 1,
    }
