"""SWE-bench benchmark adapter — instance 加载、run 管理、predictions 导出。"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger("story-lifecycle.swebench")


@dataclass
class SWEbenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    test_patch: str = ""
    version: str = ""
    FAIL_TO_PASS: list[str] | None = None
    PASS_TO_PASS: list[str] | None = None


def load_instances_jsonl(
    path: Path, limit: int | None = None
) -> list[SWEbenchInstance]:
    """从本地 JSONL 文件加载 SWE-bench instances。"""
    instances: list[SWEbenchInstance] = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            instances.append(
                SWEbenchInstance(
                    instance_id=row["instance_id"],
                    repo=row["repo"],
                    base_commit=row["base_commit"],
                    problem_statement=row["problem_statement"],
                    hints_text=row.get("hints_text", ""),
                    test_patch=row.get("test_patch", ""),
                    version=row.get("version", ""),
                    FAIL_TO_PASS=row.get("FAIL_TO_PASS"),
                    PASS_TO_PASS=row.get("PASS_TO_PASS"),
                )
            )
    return instances
