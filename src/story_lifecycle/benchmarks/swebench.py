"""SWE-bench benchmark adapter — instance 加载、run 管理、predictions 导出。"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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


_BUDGET_PRESETS = {
    "smoke": {
        "max_rounds": 1,
        "max_review_rounds": 1,
        "max_tokens_per_instance": 200_000,
        "timeout_seconds": 1800,
    },
    "standard": {
        "max_rounds": 3,
        "max_review_rounds": 2,
        "max_tokens_per_instance": 800_000,
        "timeout_seconds": 3600,
    },
    "leaderboard": {
        "max_rounds": 5,
        "max_review_rounds": 3,
        "max_tokens_per_instance": 2_000_000,
        "timeout_seconds": 7200,
    },
}


@dataclass
class BudgetConfig:
    name: str
    max_rounds: int = 1
    max_review_rounds: int = 1
    max_tokens_per_instance: int = 200_000
    timeout_seconds: int = 1800

    def __post_init__(self):
        preset = _BUDGET_PRESETS.get(self.name)
        if preset:
            self.max_rounds = preset["max_rounds"]
            self.max_review_rounds = preset["max_review_rounds"]
            self.max_tokens_per_instance = preset["max_tokens_per_instance"]
            self.timeout_seconds = preset["timeout_seconds"]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "max_rounds": self.max_rounds,
            "max_review_rounds": self.max_review_rounds,
            "max_tokens_per_instance": self.max_tokens_per_instance,
            "timeout_seconds": self.timeout_seconds,
        }


class RunStore:
    """管理 run 目录、manifest 和 instance 状态。"""

    def __init__(self, workspace_root: Path):
        self.root = Path(workspace_root)

    def create_run(
        self,
        run_id: str,
        instances: list[SWEbenchInstance],
        agent: str = "claude",
        budget: str = "smoke",
    ) -> dict:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        budget_cfg = BudgetConfig(name=budget)
        instance_entries = []
        for inst in instances:
            ws = run_dir / inst.instance_id
            ws.mkdir(exist_ok=True)
            instance_entries.append(
                {
                    "instance_id": inst.instance_id,
                    "story_key": inst.instance_id,
                    "repo": inst.repo,
                    "base_commit": inst.base_commit,
                    "workspace": str(ws),
                    "status": "prepared",
                }
            )

        manifest = {
            "run_id": run_id,
            "agent": agent,
            "profile": "swebench",
            "budget": budget_cfg.to_dict(),
            "mode": "benchmark",
            "gate_policy": "auto_fail",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "instances": instance_entries,
        }
        self._write_manifest(run_id, manifest)
        return manifest

    def update_instance(self, run_id: str, instance_id: str, **fields) -> None:
        manifest = self.load_manifest(run_id)
        for entry in manifest["instances"]:
            if entry["instance_id"] == instance_id:
                entry.update(fields)
                break
        self._write_manifest(run_id, manifest)

    def load_manifest(self, run_id: str) -> dict:
        path = self._manifest_path(run_id)
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _manifest_path(self, run_id: str) -> Path:
        return self.root / run_id / "manifest.json"

    def _write_manifest(self, run_id: str, manifest: dict) -> None:
        path = self._manifest_path(run_id)
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
