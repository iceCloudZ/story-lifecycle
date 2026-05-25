"""SWE-bench benchmark adapter 测试。"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from click.testing import CliRunner

from story_lifecycle.benchmarks.swebench import (
    BudgetConfig,
    RunStore,
    SWEbenchInstance,
    checkout_instance,
    export_predictions,
    inspect_patch_noise,
    load_instances_jsonl,
    prepare_instance,
)
from story_lifecycle.cli.main import cli


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


class TestCheckout:
    def test_checkout_creates_workspace_from_cache(self, tmp_path):
        """cache 存在时，用 clone --reference + checkout base_commit。"""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        workspace_root = tmp_path / "runs"

        inst = SWEbenchInstance("django__django-12345", "django/django", "abc123", "ps")
        run_dir = workspace_root / "r1"
        run_dir.mkdir(parents=True)
        ws = run_dir / inst.instance_id

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch(
            "story_lifecycle.benchmarks.swebench.subprocess.run", side_effect=fake_run
        ):
            result = checkout_instance(inst, ws, cache_root)

        assert result["status"] == "checked_out"
        assert len(calls) >= 2
        clone_cmd = calls[0]
        assert "clone" in clone_cmd and "--mirror" in clone_cmd

    def test_checkout_fails_on_nonzero_exit(self, tmp_path):
        """git checkout 返回非零时，返回 checkout_failed。"""
        cache_root = tmp_path / "cache"
        inst = SWEbenchInstance("inst-1", "a/b", "badcommit", "ps")
        ws = tmp_path / "ws"
        ws.mkdir()

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "fatal: bad revision"
            return r

        with patch(
            "story_lifecycle.benchmarks.swebench.subprocess.run", side_effect=fake_run
        ):
            result = checkout_instance(inst, ws, cache_root)

        assert result["status"] == "checkout_failed"
        assert "bad revision" in result["error"]

    def test_existing_workspace_fetches_and_resets(self, tmp_path):
        """已有 workspace 时 fetch + checkout + clean。"""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        inst = SWEbenchInstance("inst-1", "a/b", "c1", "ps")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch(
            "story_lifecycle.benchmarks.swebench.subprocess.run", side_effect=fake_run
        ):
            result = checkout_instance(inst, ws, cache_root)

        assert result["status"] == "checked_out"
        cmd_strs = [" ".join(c) for c in calls]
        assert any("fetch" in c for c in cmd_strs)
        assert any("checkout" in c for c in cmd_strs)


class TestPrepareInstance:
    def test_prepare_creates_prd_file(self, tmp_path, isolated_story_home):
        """prepare_instance 写 PRD markdown 到 workspace。"""
        inst = SWEbenchInstance(
            "django__django-12345",
            "django/django",
            "abc123",
            "QuerySet.none() returns incorrect results",
            hints_text="Check QuerySet.none()",
        )
        ws = tmp_path / "workspace" / inst.instance_id
        ws.mkdir(parents=True)

        prepare_instance(inst, workspace=ws, run_id="r1")

        prd_path = ws / "prd" / f"{inst.instance_id}.md"
        assert prd_path.exists()
        content = prd_path.read_text(encoding="utf-8")
        assert "django/django" in content
        assert "QuerySet.none()" in content
        assert "abc123" in content

    def test_prepare_creates_story_in_db(self, tmp_path, isolated_story_home):
        """prepare_instance 在 DB 中创建 Story。"""
        from story_lifecycle.db import models as db

        inst = SWEbenchInstance("inst-1", "a/b", "c1", "fix the bug")
        ws = tmp_path / "ws" / "inst-1"
        ws.mkdir(parents=True)

        result = prepare_instance(inst, workspace=ws, run_id="r1")

        assert result["status"] == "prepared"
        story = db.get_story("inst-1")
        assert story is not None
        assert story["profile"] == "swebench"
        assert story["workspace"] == str(ws)

    def test_prepare_sets_context_json(self, tmp_path, isolated_story_home):
        """prepare_instance 在 context_json 中写入 SWE-bench context。"""
        from story_lifecycle.db import models as db

        inst = SWEbenchInstance(
            "inst-2",
            "a/b",
            "c1",
            "fix it",
            hints_text="hint1",
            FAIL_TO_PASS=["test_a"],
        )
        ws = tmp_path / "ws" / "inst-2"
        ws.mkdir(parents=True)

        prepare_instance(inst, workspace=ws, run_id="r1")

        story = db.get_story("inst-2")
        ctx = json.loads(story["context_json"])
        assert ctx["benchmark"] == "swebench"
        assert ctx["run_id"] == "r1"
        assert ctx["instance_id"] == "inst-2"
        assert ctx["repo"] == "a/b"
        assert ctx["base_commit"] == "c1"
        assert ctx["problem_statement"] == "fix it"
        assert ctx["hints_text"] == "hint1"
        assert ctx["fail_to_pass"] == ["test_a"]

    def test_prepare_derives_title_from_problem_statement(
        self, tmp_path, isolated_story_home
    ):
        """title 从 problem_statement 第一行截取。"""
        from story_lifecycle.db import models as db

        inst = SWEbenchInstance(
            "inst-3", "a/b", "c1", "This is a long problem\nwith multiple lines"
        )
        ws = tmp_path / "ws" / "inst-3"
        ws.mkdir(parents=True)

        prepare_instance(inst, workspace=ws, run_id="r1")

        story = db.get_story("inst-3")
        assert story["title"] == "This is a long problem"


class TestExportPredictions:
    def test_export_from_finalize_json(self, tmp_path):
        """从 .story-done finalize.json 读取 model_patch。"""
        inst = SWEbenchInstance("inst-1", "a/b", "c1", "ps")
        run_dir = tmp_path / "r1"
        ws = run_dir / "inst-1"
        ws.mkdir(parents=True)
        done_dir = ws / ".story-done" / "inst-1"
        done_dir.mkdir(parents=True)
        done_dir.joinpath("finalize.json").write_text(
            json.dumps(
                {
                    "model_patch": "diff --git a/file.py b/file.py\n+fix",
                    "patch_summary": "fixed the bug",
                }
            ),
            encoding="utf-8",
        )

        store = RunStore(tmp_path)
        store.create_run(run_id="r1", instances=[inst], agent="claude")

        rows = export_predictions(store, "r1")

        assert len(rows) == 1
        assert rows[0]["instance_id"] == "inst-1"
        assert "diff --git" in rows[0]["model_patch"]

    def test_export_writes_predictions_jsonl(self, tmp_path):
        """导出写入 predictions.jsonl 文件。"""
        inst = SWEbenchInstance("inst-1", "a/b", "c1", "ps")
        run_dir = tmp_path / "r1"
        ws = run_dir / "inst-1"
        ws.mkdir(parents=True)
        done_dir = ws / ".story-done" / "inst-1"
        done_dir.mkdir(parents=True)
        done_dir.joinpath("finalize.json").write_text(
            json.dumps(
                {
                    "model_patch": "diff content",
                }
            ),
            encoding="utf-8",
        )

        store = RunStore(tmp_path)
        store.create_run(run_id="r1", instances=[inst], agent="claude")

        export_predictions(store, "r1")

        pred_path = run_dir / "predictions.jsonl"
        assert pred_path.exists()
        lines = pred_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["instance_id"] == "inst-1"
        assert row["model_name_or_path"] == "story-lifecycle-claude"

    def test_export_empty_patch_for_missing_done(self, tmp_path):
        """没有 finalize.json 时导出空 patch。"""
        inst = SWEbenchInstance("inst-1", "a/b", "c1", "ps")
        run_dir = tmp_path / "r1"
        ws = run_dir / "inst-1"
        ws.mkdir(parents=True)

        store = RunStore(tmp_path)
        store.create_run(run_id="r1", instances=[inst], agent="claude")

        rows = export_predictions(store, "r1")
        assert rows[0]["model_patch"] == ""


class TestPatchNoiseInspection:
    def test_clean_patch_passes(self):
        result = inspect_patch_noise("diff --git a/file.py\n+fix\n")
        assert "patch_too_noisy" not in result.get("tags", [])

    def test_too_many_files_flagged(self):
        patch = "\n".join(
            f"diff --git a/file{i}.py b/file{i}.py\n+change" for i in range(25)
        )
        result = inspect_patch_noise(patch)
        assert "patch_too_noisy" in result.get("tags", [])

    def test_empty_patch_not_noisy(self):
        result = inspect_patch_noise("")
        assert "patch_too_noisy" not in result.get("tags", [])

    def test_diff_size_over_1mb_flagged(self):
        big_patch = "diff --git a/big.py\n" + "+x" * 1_100_000 + "\n"
        result = inspect_patch_noise(big_patch)
        assert "patch_too_noisy" in result.get("tags", [])
        assert result["diff_size_bytes"] > 1_000_000


class TestCLI:
    def test_swebench_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["swebench", "--help"])
        assert result.exit_code == 0
        assert "prepare" in result.output
        assert "solve" in result.output
        assert "export" in result.output
        assert "run" in result.output

    def test_prepare_no_checkout(self, tmp_path, isolated_story_home):
        """prepare --no-checkout 跳过 git 操作，只创建 manifest 和 Story。"""
        instances = tmp_path / "test.jsonl"
        instances.write_text(
            json.dumps(
                {
                    "instance_id": "test-inst-1",
                    "repo": "a/b",
                    "base_commit": "c1",
                    "problem_statement": "fix bug",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        ws_root = tmp_path / "runs"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "swebench",
                "prepare",
                "--instances",
                str(instances),
                "--run-id",
                "test-r1",
                "--workspace-root",
                str(ws_root),
                "--no-checkout",
            ],
        )
        assert result.exit_code == 0
        assert "test-inst-1" in result.output
        assert (ws_root / "test-r1" / "manifest.json").exists()
