"""SWE-bench benchmark adapter 测试。"""

import json
from pathlib import Path

import pytest

from story_lifecycle.benchmarks.swebench import (
    BudgetConfig,
    RunStore,
    SWEbenchInstance,
    load_instances_jsonl,
)


FIXTURES = Path(__file__).parent / "fixtures"


class TestLoadInstances:
    def test_load_single_instance(self):
        path = FIXTURES / "swebench-one.jsonl"
        instances = load_instances_jsonl(path)
        assert len(instances) == 1
        inst = instances[0]
        assert inst.instance_id == "django__django-12345"
        assert inst.repo == "django/django"
        assert inst.base_commit == "abc123def456"
        assert "QuerySet.none()" in inst.problem_statement
        assert inst.hints_text == "Look at QuerySet.none() implementation"
        assert inst.FAIL_TO_PASS == ["test_none_chained"]
        assert inst.PASS_TO_PASS == ["test_basic"]

    def test_load_multiple_instances(self, tmp_path):
        lines = [
            json.dumps(
                {
                    "instance_id": "a__b-1",
                    "repo": "a/b",
                    "base_commit": "c1",
                    "problem_statement": "ps1",
                }
            ),
            json.dumps(
                {
                    "instance_id": "a__b-2",
                    "repo": "a/b",
                    "base_commit": "c2",
                    "problem_statement": "ps2",
                }
            ),
        ]
        f = tmp_path / "multi.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        instances = load_instances_jsonl(f)
        assert len(instances) == 2
        assert instances[0].instance_id == "a__b-1"
        assert instances[1].instance_id == "a__b-2"

    def test_limit_instances(self, tmp_path):
        lines = [
            json.dumps(
                {
                    "instance_id": f"inst-{i}",
                    "repo": "a/b",
                    "base_commit": "c",
                    "problem_statement": f"ps{i}",
                }
            )
            for i in range(10)
        ]
        f = tmp_path / "many.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        instances = load_instances_jsonl(f, limit=3)
        assert len(instances) == 3

    def test_missing_optional_fields_get_defaults(self, tmp_path):
        f = tmp_path / "minimal.jsonl"
        f.write_text(
            json.dumps(
                {
                    "instance_id": "x__y-1",
                    "repo": "x/y",
                    "base_commit": "c",
                    "problem_statement": "ps",
                }
            ),
            encoding="utf-8",
        )
        instances = load_instances_jsonl(f)
        inst = instances[0]
        assert inst.hints_text == ""
        assert inst.test_patch == ""
        assert inst.version == ""
        assert inst.FAIL_TO_PASS is None
        assert inst.PASS_TO_PASS is None


class TestRunStore:
    def test_create_run_creates_manifest(self, tmp_path):
        store = RunStore(tmp_path)
        manifest = store.create_run(
            run_id="smoke-001",
            instances=[
                SWEbenchInstance(
                    "django__django-12345", "django/django", "abc123", "ps"
                ),
            ],
            agent="claude",
            budget="smoke",
        )
        assert manifest["run_id"] == "smoke-001"
        assert manifest["agent"] == "claude"
        assert manifest["budget"]["name"] == "smoke"
        assert len(manifest["instances"]) == 1
        assert manifest["instances"][0]["instance_id"] == "django__django-12345"
        assert manifest["instances"][0]["status"] == "prepared"

    def test_create_run_writes_manifest_file(self, tmp_path):
        store = RunStore(tmp_path)
        store.create_run(run_id="smoke-001", instances=[], agent="claude")
        manifest_path = tmp_path / "smoke-001" / "manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["run_id"] == "smoke-001"

    def test_create_run_creates_instance_workspaces(self, tmp_path):
        store = RunStore(tmp_path)
        store.create_run(
            run_id="r1",
            instances=[
                SWEbenchInstance("django__django-12345", "django/django", "abc", "ps"),
                SWEbenchInstance("flask__flask-99", "flask/flask", "def", "ps2"),
            ],
            agent="claude",
        )
        assert (tmp_path / "r1" / "django__django-12345").is_dir()
        assert (tmp_path / "r1" / "flask__flask-99").is_dir()

    def test_update_instance_status(self, tmp_path):
        store = RunStore(tmp_path)
        store.create_run(
            run_id="r1",
            instances=[SWEbenchInstance("inst-1", "a/b", "c", "ps")],
            agent="claude",
        )
        store.update_instance(
            "r1",
            "inst-1",
            status="checkout_failed",
            failure_type="checkout_failure",
            error="network",
        )
        manifest = store.load_manifest("r1")
        assert manifest["instances"][0]["status"] == "checkout_failed"
        assert manifest["instances"][0]["failure_type"] == "checkout_failure"

    def test_load_manifest_not_found(self, tmp_path):
        store = RunStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.load_manifest("nonexistent")

    def test_budget_smoke_defaults(self):
        cfg = BudgetConfig(name="smoke")
        assert cfg.max_rounds == 1
        assert cfg.timeout_seconds == 1800

    def test_budget_leaderboard(self):
        cfg = BudgetConfig(name="leaderboard")
        assert cfg.max_rounds == 5
        assert cfg.timeout_seconds == 7200
