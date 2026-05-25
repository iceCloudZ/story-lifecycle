"""SWE-bench benchmark adapter 测试。"""

import json
from pathlib import Path

from story_lifecycle.benchmarks.swebench import load_instances_jsonl


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
