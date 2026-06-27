# SWE-bench Runner 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 实现 SWE-bench benchmark adapter，将 SWE-bench instance 映射为 Story，通过现有 orchestrator 执行，导出官方兼容的 predictions.jsonl。

**架构：** 薄层 `benchmarks.swebench` 模块负责 instance 加载、确定性 git checkout（含 clone cache）、run manifest 管理、predictions 导出。`cli.swebench` Click group 接入 `story` CLI。每个 instance 作为一个普通 Story，使用 `swebench` profile（含 finalize stage）。

**技术栈：** Python 3.11+, Click, SQLite, Git (subprocess), filelock, PyYAML

---

## 文件结构

### 新建文件

| 文件 | 职责 |
|------|------|
| `src/story_lifecycle/benchmarks/__init__.py` | 包初始化 |
| `src/story_lifecycle/benchmarks/swebench.py` | Instance 数据类、JSONL 加载、RunStore、checkout、story adapter、predictions 导出 |
| `src/story_lifecycle/cli/swebench.py` | Click 命令组 `story swebench` |
| `profiles/swebench.yaml` | Benchmark profile，含 finalize stage |
| `prompts/swebench_finalize.md` | Finalize prompt 模板 |
| `tests/test_swebench.py` | 全部单元测试 |
| `tests/fixtures/swebench-one.jsonl` | 测试 fixture：单条 instance JSONL |

### 修改文件

| 文件 | 改动 |
|------|------|
| `src/story_lifecycle/cli/main.py` | 导入并注册 `swebench_group` |
| `pyproject.toml` | 添加 `filelock` 依赖 |

---

## Task 1: SWEbenchInstance 数据类 + JSONL 加载器

**文件：**
- 新建: `src/story_lifecycle/benchmarks/__init__.py`
- 新建: `src/story_lifecycle/benchmarks/swebench.py`
- 新建: `tests/test_swebench.py`
- 新建: `tests/fixtures/swebench-one.jsonl`

- [ ] **Step 1: 创建测试 fixture JSONL**

```text
tests/fixtures/swebench-one.jsonl
```

```json
{"instance_id": "django__django-12345", "repo": "django/django", "base_commit": "abc123def456", "problem_statement": "QuerySet.none() returns incorrect results when chained with filter()", "hints_text": "Look at QuerySet.none() implementation", "test_patch": "diff --git a/tests/test_queryset.py b/tests/test_queryset.py\n", "version": "3.0", "FAIL_TO_PASS": ["test_none_chained"], "PASS_TO_PASS": ["test_basic"]}
```

- [ ] **Step 2: 编写 JSONL 加载失败测试**

```python
# tests/test_swebench.py
"""SWE-bench benchmark adapter 测试。"""

import json
from pathlib import Path

import pytest

from story_lifecycle.benchmarks.swebench import SWEbenchInstance, load_instances_jsonl


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
            json.dumps({
                "instance_id": "a__b-1", "repo": "a/b",
                "base_commit": "c1", "problem_statement": "ps1",
            }),
            json.dumps({
                "instance_id": "a__b-2", "repo": "a/b",
                "base_commit": "c2", "problem_statement": "ps2",
            }),
        ]
        f = tmp_path / "multi.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        instances = load_instances_jsonl(f)
        assert len(instances) == 2
        assert instances[0].instance_id == "a__b-1"
        assert instances[1].instance_id == "a__b-2"

    def test_limit_instances(self, tmp_path):
        lines = [
            json.dumps({
                "instance_id": f"inst-{i}", "repo": "a/b",
                "base_commit": "c", "problem_statement": f"ps{i}",
            })
            for i in range(10)
        ]
        f = tmp_path / "many.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        instances = load_instances_jsonl(f, limit=3)
        assert len(instances) == 3

    def test_missing_optional_fields_get_defaults(self, tmp_path):
        f = tmp_path / "minimal.jsonl"
        f.write_text(json.dumps({
            "instance_id": "x__y-1", "repo": "x/y",
            "base_commit": "c", "problem_statement": "ps",
        }), encoding="utf-8")
        instances = load_instances_jsonl(f)
        inst = instances[0]
        assert inst.hints_text == ""
        assert inst.test_patch == ""
        assert inst.version == ""
        assert inst.FAIL_TO_PASS is None
        assert inst.PASS_TO_PASS is None
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/test_swebench.py::TestLoadInstances -v`
Expected: FAIL — `ModuleNotFoundError: story_lifecycle.benchmarks`

- [ ] **Step 4: 创建 benchmarks 包并实现**

```python
# src/story_lifecycle/benchmarks/__init__.py
"""Benchmark adapters for Story Lifecycle Manager."""
```

```python
# src/story_lifecycle/benchmarks/swebench.py
"""SWE-bench benchmark adapter — instance 加载、run 管理、predictions 导出。"""

import json
import logging
import subprocess
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


def load_instances_jsonl(path: Path, limit: int | None = None) -> list[SWEbenchInstance]:
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
            instances.append(SWEbenchInstance(
                instance_id=row["instance_id"],
                repo=row["repo"],
                base_commit=row["base_commit"],
                problem_statement=row["problem_statement"],
                hints_text=row.get("hints_text", ""),
                test_patch=row.get("test_patch", ""),
                version=row.get("version", ""),
                FAIL_TO_PASS=row.get("FAIL_TO_PASS"),
                PASS_TO_PASS=row.get("PASS_TO_PASS"),
            ))
    return instances
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_swebench.py::TestLoadInstances -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/story_lifecycle/benchmarks/__init__.py src/story_lifecycle/benchmarks/swebench.py tests/test_swebench.py tests/fixtures/swebench-one.jsonl
git commit -m "feat(swebench): 添加 SWEbenchInstance 数据类和 JSONL 加载器"
```

---

## Task 2: RunStore — Manifest + 目录管理

**文件：**
- 修改: `src/story_lifecycle/benchmarks/swebench.py`
- 修改: `tests/test_swebench.py`

- [ ] **Step 1: 编写 RunStore 失败测试**

追加到 `tests/test_swebench.py`：

```python
from story_lifecycle.benchmarks.swebench import RunStore, BudgetConfig


class TestRunStore:
    def test_create_run_creates_manifest(self, tmp_path):
        store = RunStore(tmp_path)
        manifest = store.create_run(
            run_id="smoke-001",
            instances=[
                SWEbenchInstance("django__django-12345", "django/django", "abc123", "ps"),
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
        store.update_instance("r1", "inst-1", status="checkout_failed", failure_type="checkout_failure", error="network")
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_swebench.py::TestRunStore -v`
Expected: FAIL — `ImportError: cannot import name 'RunStore'`

- [ ] **Step 3: 实现 RunStore 和 BudgetConfig**

追加到 `src/story_lifecycle/benchmarks/swebench.py`：

```python
_BUDGET_PRESETS = {
    "smoke": {"max_rounds": 1, "max_review_rounds": 1, "max_tokens_per_instance": 200_000, "timeout_seconds": 1800},
    "standard": {"max_rounds": 3, "max_review_rounds": 2, "max_tokens_per_instance": 800_000, "timeout_seconds": 3600},
    "leaderboard": {"max_rounds": 5, "max_review_rounds": 3, "max_tokens_per_instance": 2_000_000, "timeout_seconds": 7200},
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
            instance_entries.append({
                "instance_id": inst.instance_id,
                "story_key": inst.instance_id,
                "repo": inst.repo,
                "base_commit": inst.base_commit,
                "workspace": str(ws),
                "status": "prepared",
            })

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
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_swebench.py::TestRunStore -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/story_lifecycle/benchmarks/swebench.py tests/test_swebench.py
git commit -m "feat(swebench): 添加 RunStore manifest 和 BudgetConfig 管理"
```

---

## Task 3: Repo Checkout + Clone Cache

**文件：**
- 修改: `src/story_lifecycle/benchmarks/swebench.py`
- 修改: `tests/test_swebench.py`

- [ ] **Step 1: 编写 checkout 失败测试**

追加到 `tests/test_swebench.py`：

```python
from unittest.mock import patch, MagicMock

from story_lifecycle.benchmarks.swebench import checkout_instance


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

        with patch("story_lifecycle.benchmarks.swebench.subprocess.run", side_effect=fake_run):
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

        with patch("story_lifecycle.benchmarks.swebench.subprocess.run", side_effect=fake_run):
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
        (ws / ".git").mkdir()  # 模拟已有 git repo

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("story_lifecycle.benchmarks.swebench.subprocess.run", side_effect=fake_run):
            result = checkout_instance(inst, ws, cache_root)

        assert result["status"] == "checked_out"
        cmd_strs = [" ".join(c) for c in calls]
        assert any("fetch" in c for c in cmd_strs)
        assert any("checkout" in c for c in cmd_strs)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_swebench.py::TestCheckout -v`
Expected: FAIL — `ImportError: cannot import name 'checkout_instance'`

- [ ] **Step 3: 实现 checkout_instance**

追加到 `src/story_lifecycle/benchmarks/swebench.py`：

```python
def _repo_slug(repo: str) -> str:
    """'owner/name' -> 'owner__name'，用于文件系统路径。"""
    return repo.replace("/", "__")


def _run_git(*args: str, cwd: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = ["git"] + list(args)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _ensure_cache(cache_root: Path, repo: str) -> Path:
    """确保 mirror cache 存在。返回 cache repo 路径。"""
    slug = _repo_slug(repo)
    cache_repo = cache_root / slug
    cache_repo.parent.mkdir(parents=True, exist_ok=True)

    if cache_repo.exists():
        log.info("Updating cache for %s", repo)
        r = _run_git("remote", "update", "--prune", cwd=str(cache_repo))
        if r.returncode != 0:
            log.warning("Cache update failed for %s: %s", repo, r.stderr)
    else:
        log.info("Creating mirror cache for %s", repo)
        repo_url = f"https://github.com/{repo}.git"
        r = _run_git("clone", "--mirror", repo_url, str(cache_repo))
        if r.returncode != 0:
            raise RuntimeError(f"Mirror clone failed for {repo}: {r.stderr}")

    return cache_repo


def checkout_instance(
    inst: SWEbenchInstance,
    workspace: Path,
    cache_root: Path,
) -> dict:
    """将 SWE-bench instance 的 repo checkout 到 base_commit。

    返回结果 dict，包含 status 和可选的 error。
    """
    workspace = Path(workspace)
    repo_url = f"https://github.com/{inst.repo}.git"

    try:
        # 确保 mirror cache
        try:
            cache_repo = _ensure_cache(cache_root, inst.repo)
            cache_available = True
        except RuntimeError as e:
            log.warning("Cache setup 失败，回退到直接 clone: %s", e)
            cache_available = False

        if workspace.exists() and (workspace / ".git").exists():
            # 已有 workspace — fetch + reset
            r = _run_git("fetch", "origin", cwd=str(workspace))
            if r.returncode != 0:
                return {"status": "checkout_failed", "error": f"fetch failed: {r.stderr}"}
            r = _run_git("checkout", inst.base_commit, cwd=str(workspace))
            if r.returncode != 0:
                return {"status": "checkout_failed", "error": f"checkout failed: {r.stderr}"}
            _run_git("clean", "-fdx", cwd=str(workspace))
        else:
            # 全新 clone
            workspace.parent.mkdir(parents=True, exist_ok=True)
            if cache_available:
                r = _run_git(
                    "clone", "--reference", str(cache_repo),
                    repo_url, str(workspace),
                )
            else:
                r = _run_git("clone", repo_url, str(workspace))
            if r.returncode != 0:
                return {"status": "checkout_failed", "error": f"clone failed: {r.stderr}"}

            r = _run_git("fetch", "origin", inst.base_commit, cwd=str(workspace))
            if r.returncode != 0:
                return {"status": "checkout_failed", "error": f"fetch commit failed: {r.stderr}"}
            r = _run_git("checkout", inst.base_commit, cwd=str(workspace))
            if r.returncode != 0:
                return {"status": "checkout_failed", "error": f"checkout failed: {r.stderr}"}

        return {"status": "checked_out"}

    except Exception as e:
        return {"status": "checkout_failed", "error": str(e)}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_swebench.py::TestCheckout -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/story_lifecycle/benchmarks/swebench.py tests/test_swebench.py
git commit -m "feat(swebench): 添加 repo checkout 和 clone cache 逻辑"
```

---

## Task 4: Instance → Story 映射（PRD + context_json）

**文件：**
- 修改: `src/story_lifecycle/benchmarks/swebench.py`
- 修改: `tests/test_swebench.py`

- [ ] **Step 1: 编写 Story 映射失败测试**

追加到 `tests/test_swebench.py`：

```python
from story_lifecycle.benchmarks.swebench import prepare_instance


class TestPrepareInstance:
    def test_prepare_creates_prd_file(self, tmp_path, isolated_story_home):
        """prepare_instance 写 PRD markdown 到 workspace。"""
        inst = SWEbenchInstance(
            "django__django-12345", "django/django", "abc123",
            "QuerySet.none() returns incorrect results",
            hints_text="Check QuerySet.none()",
        )
        ws = tmp_path / "workspace" / inst.instance_id
        ws.mkdir(parents=True)

        result = prepare_instance(inst, workspace=ws, run_id="r1")

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
            "inst-2", "a/b", "c1", "fix it",
            hints_text="hint1", FAIL_TO_PASS=["test_a"],
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

    def test_prepare_derives_title_from_problem_statement(self, tmp_path, isolated_story_home):
        """title 从 problem_statement 第一行截取。"""
        from story_lifecycle.db import models as db

        inst = SWEbenchInstance("inst-3", "a/b", "c1", "This is a long problem\nwith multiple lines")
        ws = tmp_path / "ws" / "inst-3"
        ws.mkdir(parents=True)

        prepare_instance(inst, workspace=ws, run_id="r1")

        story = db.get_story("inst-3")
        assert story["title"] == "This is a long problem"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_swebench.py::TestPrepareInstance -v`
Expected: FAIL — `ImportError: cannot import name 'prepare_instance'`

- [ ] **Step 3: 实现 prepare_instance**

追加到 `src/story_lifecycle/benchmarks/swebench.py`：

```python
from ..db import models as db


def prepare_instance(
    inst: SWEbenchInstance,
    workspace: Path,
    run_id: str,
    agent: str = "claude",
) -> dict:
    """将 SWE-bench instance 映射为 Story：写 PRD、设 context_json、创建 DB 记录。

    返回结果 dict，包含 status 和可选的 error。
    """
    workspace = Path(workspace)

    # 1. 写 PRD markdown
    prd_dir = workspace / "prd"
    prd_dir.mkdir(parents=True, exist_ok=True)
    prd_path = prd_dir / f"{inst.instance_id}.md"
    prd_content = _render_prd(inst)
    prd_path.write_text(prd_content, encoding="utf-8")

    # 2. 构造 title（取 problem_statement 第一行，最多 80 字符）
    title = inst.problem_statement.split("\n")[0][:80]

    # 3. 构造 context_json
    context = {
        "benchmark": "swebench",
        "run_id": run_id,
        "instance_id": inst.instance_id,
        "repo": inst.repo,
        "base_commit": inst.base_commit,
        "problem_statement": inst.problem_statement,
        "hints_text": inst.hints_text,
        "test_patch": inst.test_patch,
        "fail_to_pass": inst.FAIL_TO_PASS or [],
        "pass_to_pass": inst.PASS_TO_PASS or [],
        "prd_path": str(prd_path),
    }

    # 4. 创建 Story
    db.upsert_story(
        story_key=inst.instance_id,
        title=title,
        workspace=str(workspace),
        profile="swebench",
        current_stage="design",
        status="active",
    )
    db.update_story(inst.instance_id, context_json=json.dumps(context, ensure_ascii=False))
    db.update_story(inst.instance_id, source_type="swebench", source_id=inst.instance_id)

    return {"status": "prepared", "story_key": inst.instance_id}


def _render_prd(inst: SWEbenchInstance) -> str:
    """渲染 SWE-bench instance 为 PRD markdown。"""
    sections = [
        f"# SWE-bench Instance: {inst.instance_id}",
        "",
        f"## Repository",
        "",
        inst.repo,
        "",
        f"## Base Commit",
        "",
        inst.base_commit,
        "",
        f"## Problem Statement",
        "",
        inst.problem_statement,
    ]
    if inst.hints_text:
        sections += ["", "## Hints", "", inst.hints_text]
    if inst.test_patch:
        sections += ["", "## Test Patch", "", inst.test_patch]
    if inst.FAIL_TO_PASS:
        sections += ["", "## Failing Tests", ""] + [f"- {t}" for t in inst.FAIL_TO_PASS]
    return "\n".join(sections) + "\n"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_swebench.py::TestPrepareInstance -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/story_lifecycle/benchmarks/swebench.py tests/test_swebench.py
git commit -m "feat(swebench): 添加 instance → Story 映射（PRD + context_json）"
```

---

## Task 5: Profile + Finalize Prompt

**文件：**
- 新建: `profiles/swebench.yaml`
- 新建: `prompts/swebench_finalize.md`

- [ ] **Step 1: 创建 swebench profile**

```yaml
# profiles/swebench.yaml
# SWE-bench benchmark profile — design → implement → test → finalize
version: 2
cli: claude

stages:
  design:
    order: 1
    description: "问题定位与方案设计"
    confirm: false
    review: true
    max_retries: 1
    expected_outputs:
      - root_cause
      - target_files
      - test_strategy
    next_default: [implement]

  implement:
    order: 2
    description: "编码实现"
    confirm: false
    review: true
    max_retries: 2
    expected_outputs:
      - patch_summary
    next_default: [test]

  test:
    order: 3
    description: "验证测试"
    confirm: false
    review: false
    max_retries: 1
    expected_outputs:
      - test_command
      - test_result
    next_default: [finalize]

  finalize:
    order: 4
    description: "生成最终 patch"
    confirm: false
    review: false
    max_retries: 1
    expected_outputs:
      - model_patch
    next_default: []

quality:
  enabled: false

adversarial:
  enabled: false
```

- [ ] **Step 2: 创建 finalize prompt**

```markdown
<!-- prompts/swebench_finalize.md -->
生成最终 patch 用于 SWE-bench 评估。

## 任务信息

- Story Key: {story_key}
- 标题: {title}
{prd_path_section}

## 步骤

1. 运行 `git diff` 查看当前所有改动
2. 确认 diff 只包含修复核心逻辑所需的改动
3. 如果 diff 包含无关文件（日志、临时文件、本地配置、格式化无关改动、依赖缓存、测试产物），必须先清理
4. 运行 `git diff --stat` 确认 diff 干净

## 完成后

将结果写入项目根目录下的 `.story-done/{story_key}/finalize.json`：

```json
{
  "model_patch": "完整的 git diff 输出",
  "patch_summary": "一句话描述修复内容"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks。
> CRITICAL: model_patch 只包含修复核心逻辑所需的 diff，不要包含任何无关改动。
```

- [ ] **Step 3: 验证 profile 可加载**

手动验证（无自动化测试）：
Run: `python -c "from story_lifecycle.orchestrator.nodes import load_profile; p = load_profile('swebench'); print(list(p['stages'].keys()))"`
Expected: `['design', 'implement', 'test', 'finalize']`

- [ ] **Step 4: 提交**

```bash
git add profiles/swebench.yaml prompts/swebench_finalize.md
git commit -m "feat(swebench): 添加 swebench profile 和 finalize prompt"
```

---

## Task 6: Predictions 导出 + Patch Noise Inspection

**文件：**
- 修改: `src/story_lifecycle/benchmarks/swebench.py`
- 修改: `tests/test_swebench.py`

- [ ] **Step 1: 编写导出和 noise inspection 失败测试**

追加到 `tests/test_swebench.py`：

```python
from story_lifecycle.benchmarks.swebench import export_predictions, inspect_patch_noise


class TestExportPredictions:
    def test_export_from_finalize_json(self, tmp_path):
        """从 .story-done finalize.json 读取 model_patch。"""
        inst = SWEbenchInstance("inst-1", "a/b", "c1", "ps")
        run_dir = tmp_path / "r1"
        ws = run_dir / "inst-1"
        ws.mkdir(parents=True)
        done_dir = ws / ".story-done" / "inst-1"
        done_dir.mkdir(parents=True)
        done_dir.joinpath("finalize.json").write_text(json.dumps({
            "model_patch": "diff --git a/file.py b/file.py\n+fix",
            "patch_summary": "fixed the bug",
        }), encoding="utf-8")

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
        done_dir.joinpath("finalize.json").write_text(json.dumps({
            "model_patch": "diff content",
        }), encoding="utf-8")

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
            f"diff --git a/file{i}.py b/file{i}.py\n+change"
            for i in range(25)
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_swebench.py::TestExportPredictions tests/test_swebench.py::TestPatchNoiseInspection -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现 export_predictions 和 inspect_patch_noise**

追加到 `src/story_lifecycle/benchmarks/swebench.py`：

```python
def inspect_patch_noise(patch: str) -> dict:
    """检查 patch 是否过脏。

    返回 dict:
    - tags: list[str] — 包含 'patch_too_noisy' 如果触发噪音规则
    - modified_file_count, added_file_count, deleted_file_count, binary_file_count, diff_size_bytes
    """
    if not patch:
        return {"tags": [], "modified_file_count": 0, "diff_size_bytes": 0}

    diff_size = len(patch.encode("utf-8"))
    modified = patch.count("diff --git a/")
    added = patch.count("new file mode")
    deleted = patch.count("deleted file mode")
    binary = patch.count("Binary files")

    tags: list[str] = []
    if modified > 20:
        tags.append("patch_too_noisy")
    if diff_size > 1_000_000:
        tags.append("patch_too_noisy")
    if binary > 0:
        tags.append("patch_too_noisy")

    return {
        "tags": tags,
        "modified_file_count": modified,
        "added_file_count": added,
        "deleted_file_count": deleted,
        "binary_file_count": binary,
        "diff_size_bytes": diff_size,
    }


def _read_model_patch(workspace: Path, story_key: str) -> str:
    """从 workspace 中提取 model_patch。

    优先级:
    1. .story-done/{story_key}/finalize.json 中的 model_patch
    2. workspace/final.patch
    3. 空字符串
    """
    done_file = workspace / ".story-done" / story_key / "finalize.json"
    if done_file.exists():
        try:
            data = json.loads(done_file.read_text(encoding="utf-8"))
            return data.get("model_patch", "")
        except (json.JSONDecodeError, KeyError):
            pass

    final_patch = workspace / "final.patch"
    if final_patch.exists():
        return final_patch.read_text(encoding="utf-8")

    return ""


def export_predictions(store: RunStore, run_id: str, agent: str = "claude") -> list[dict]:
    """导出 predictions.jsonl。返回 rows 列表并写入文件。"""
    manifest = store.load_manifest(run_id)
    rows = []
    for entry in manifest["instances"]:
        workspace = Path(entry["workspace"])
        instance_id = entry["instance_id"]
        story_key = entry["story_key"]

        patch = _read_model_patch(workspace, story_key)
        noise = inspect_patch_noise(patch)

        row = {
            "instance_id": instance_id,
            "model_name_or_path": f"story-lifecycle-{agent}",
            "model_patch": patch,
        }
        rows.append(row)

        # 更新 manifest 中的 noise 标签
        if noise.get("tags"):
            store.update_instance(run_id, instance_id, noise_tags=noise["tags"])

    # 写 predictions.jsonl
    pred_path = store.root / run_id / "predictions.jsonl"
    with open(pred_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return rows
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_swebench.py::TestExportPredictions tests/test_swebench.py::TestPatchNoiseInspection -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/story_lifecycle/benchmarks/swebench.py tests/test_swebench.py
git commit -m "feat(swebench): 添加 predictions 导出和 patch noise inspection"
```

---

## Task 7: CLI 命令组 `story swebench`

**文件：**
- 新建: `src/story_lifecycle/cli/swebench.py`
- 修改: `src/story_lifecycle/cli/main.py`

- [ ] **Step 1: 创建 CLI 命令组**

```python
# src/story_lifecycle/cli/swebench.py
"""`story swebench` — SWE-bench benchmark runner 命令组。"""

import click
from pathlib import Path
from rich.console import Console
from rich.table import Table

from ..benchmarks.swebench import (
    load_instances_jsonl,
    RunStore,
    checkout_instance,
    prepare_instance,
    export_predictions,
)
from ..db.models import init_db

console = Console()


@click.group(name="swebench")
def swebench_group():
    """SWE-bench benchmark runner。"""
    init_db()


@swebench_group.command()
@click.option("--instances", type=click.Path(exists=True, path_type=Path), required=True,
              help="本地 JSONL instance 文件路径")
@click.option("--run-id", required=True, help="Run ID")
@click.option("--workspace-root", type=click.Path(path_type=Path),
              default=Path(".story-runs/swebench"), help="Run 根目录")
@click.option("--agent", default="claude", help="Agent 名称")
@click.option("--budget", default="smoke", type=click.Choice(["smoke", "standard", "leaderboard"]),
              help="预算档位")
@click.option("--limit", type=int, default=None, help="最大 instance 数量")
@click.option("--cache-root", type=click.Path(path_type=Path),
              default=Path.home() / ".cache" / "story-lifecycle" / "swebench" / "repos",
              help="Clone cache 根目录")
@click.option("--no-checkout", is_flag=True, help="跳过 git checkout（仅创建 manifest 和 Story）")
def prepare(instances, run_id, workspace_root, agent, budget, limit, cache_root, no_checkout):
    """准备 SWE-bench run：加载 instances、checkout repos、创建 Stories。"""
    console.print(f"[bold]加载 instances:[/] {instances}")
    inst_list = load_instances_jsonl(instances, limit=limit)
    console.print(f"  共 {len(inst_list)} 个 instances")

    store = RunStore(workspace_root)
    manifest = store.create_run(run_id=run_id, instances=inst_list, agent=agent, budget=budget)
    console.print(f"  Run 目录: [dim]{workspace_root / run_id}[/]")

    prepared = 0
    failed = 0
    for inst in inst_list:
        ws = workspace_root / run_id / inst.instance_id

        # Git checkout
        if not no_checkout:
            result = checkout_instance(inst, ws, cache_root)
            if result["status"] == "checkout_failed":
                console.print(f"  [red]✗[/] {inst.instance_id}: checkout 失败 — {result['error'][:60]}")
                store.update_instance(run_id, inst.instance_id,
                    status="checkout_failed", failure_type="checkout_failure", error=result["error"])
                failed += 1
                continue

        # Story 映射
        result = prepare_instance(inst, workspace=ws, run_id=run_id, agent=agent)
        console.print(f"  [green]✓[/] {inst.instance_id}")
        prepared += 1

    console.print(f"\n[bold]准备完成:[/] {prepared} 成功, {failed} 失败")
    console.print(f"Manifest: [dim]{workspace_root / run_id / 'manifest.json'}[/]")


@swebench_group.command()
@click.option("--run-id", required=True, help="Run ID")
@click.option("--workspace-root", type=click.Path(path_type=Path),
              default=Path(".story-runs/swebench"), help="Run 根目录")
def solve(run_id, workspace_root):
    """启动所有 prepared instances 的 Story 执行。"""
    from ..orchestrator.graph import start_story_async

    store = RunStore(workspace_root)
    manifest = store.load_manifest(run_id)

    started = 0
    for entry in manifest["instances"]:
        if entry["status"] != "prepared":
            continue
        try:
            start_story_async(entry["story_key"])
            store.update_instance(run_id, entry["instance_id"], status="running")
            console.print(f"  [green]→[/] {entry['instance_id']} 已启动")
            started += 1
        except Exception as e:
            console.print(f"  [red]✗[/] {entry['instance_id']}: {e}")
            store.update_instance(run_id, entry["instance_id"],
                status="failed", failure_type="start_failure", error=str(e))

    console.print(f"\n[bold]已启动 {started} 个 instances[/]")


@swebench_group.command()
@click.option("--run-id", required=True, help="Run ID")
@click.option("--workspace-root", type=click.Path(path_type=Path),
              default=Path(".story-runs/swebench"), help="Run 根目录")
@click.option("--agent", default="claude", help="Agent 名称")
def export(run_id, workspace_root, agent):
    """导出 predictions.jsonl。"""
    store = RunStore(workspace_root)
    rows = export_predictions(store, run_id, agent=agent)
    pred_path = workspace_root / run_id / "predictions.jsonl"
    console.print(f"[bold]导出完成:[/] {len(rows)} predictions")
    console.print(f"  文件: [dim]{pred_path}[/]")

    # 打印 noise 警告
    manifest = store.load_manifest(run_id)
    noisy = [e for e in manifest["instances"] if e.get("noise_tags")]
    if noisy:
        console.print(f"\n[yellow]⚠ {len(noisy)} 个 patches 触发噪音检测:[/]")
        for e in noisy:
            console.print(f"  - {e['instance_id']}: {', '.join(e['noise_tags'])}")


@swebench_group.command("summarize")
@click.option("--run-id", required=True, help="Run ID")
@click.option("--workspace-root", type=click.Path(path_type=Path),
              default=Path(".story-runs/swebench"), help="Run 根目录")
def summarize_cmd(run_id, workspace_root):
    """生成 run summary。"""
    from ..db import models as db

    store = RunStore(workspace_root)
    manifest = store.load_manifest(run_id)

    total = len(manifest["instances"])
    by_status: dict[str, int] = {}
    by_failure: dict[str, int] = {}

    for entry in manifest["instances"]:
        status = entry.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        ft = entry.get("failure_type")
        if ft:
            by_failure[ft] = by_failure.get(ft, 0) + 1

    predictions_path = workspace_root / run_id / "predictions.jsonl"
    pred_count = 0
    if predictions_path.exists():
        for line in predictions_path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                pred_count += 1

    summary = {
        "run_id": run_id,
        "total": total,
        **by_status,
        "predictions": pred_count,
        "failures": by_failure,
    }

    # 写 summary.json
    summary_path = workspace_root / run_id / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    console.print(f"[bold]Run Summary: {run_id}[/]")
    console.print(f"  Total: {total}")
    for status, count in by_status.items():
        console.print(f"  {status}: {count}")
    console.print(f"  Predictions: {pred_count}")
    if by_failure:
        console.print(f"  [red]Failures:[/]")
        for ft, count in by_failure.items():
            console.print(f"    {ft}: {count}")
    console.print(f"\n  [dim]{summary_path}[/]")


@swebench_group.command()
@click.option("--instances", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--run-id", required=True, help="Run ID")
@click.option("--workspace-root", type=click.Path(path_type=Path),
              default=Path(".story-runs/swebench"), help="Run 根目录")
@click.option("--agent", default="claude")
@click.option("--budget", default="smoke", type=click.Choice(["smoke", "standard", "leaderboard"]))
@click.option("--limit", type=int, default=None)
@click.option("--no-start", is_flag=True, help="只 prepare 不 solve")
@click.option("--no-checkout", is_flag=True, help="跳过 git checkout")
@click.option("--no-evaluate", is_flag=True, default=True, help="不调用官方 harness（P0 默认）")
@click.option("--evaluate", is_flag=True, default=False, help="调用官方 harness")
@click.pass_context
def run(ctx, instances, run_id, workspace_root, agent, budget, limit,
        no_start, no_checkout, no_evaluate, evaluate):
    """完整 run：prepare → solve → export → summarize。"""
    # prepare
    ctx.invoke(prepare, instances=instances, run_id=run_id,
               workspace_root=workspace_root, agent=agent, budget=budget,
               limit=limit, cache_root=Path.home() / ".cache" / "story-lifecycle" / "swebench" / "repos",
               no_checkout=no_checkout)

    if not no_start:
        # solve
        ctx.invoke(solve, run_id=run_id, workspace_root=workspace_root)

    # export
    ctx.invoke(export, run_id=run_id, workspace_root=workspace_root, agent=agent)

    # summarize
    ctx.invoke(summarize_cmd, run_id=run_id, workspace_root=workspace_root)
```

注意：`run` 命令中 `summarize` 子命令和 `summarize_cmd` 函数命名不同，避免 Python 函数名和 Click 命令名冲突。

- [ ] **Step 2: 注册 swebench_group 到主 CLI**

在 `src/story_lifecycle/cli/main.py` 末尾、`if __name__` 之前添加：

```python
from .swebench import swebench_group  # noqa: E402

cli.add_command(swebench_group)
```

- [ ] **Step 3: 验证 CLI 注册成功**

Run: `python -m story_lifecycle swebench --help`
Expected: 显示 `prepare`, `solve`, `export`, `summarize`, `run` 子命令

- [ ] **Step 4: 添加 CLI smoke 测试**

追加到 `tests/test_swebench.py`：

```python
from click.testing import CliRunner
from story_lifecycle.cli.main import cli


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
        instances.write_text(json.dumps({
            "instance_id": "test-inst-1", "repo": "a/b",
            "base_commit": "c1", "problem_statement": "fix bug",
        }) + "\n", encoding="utf-8")

        ws_root = tmp_path / "runs"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "swebench", "prepare",
            "--instances", str(instances),
            "--run-id", "test-r1",
            "--workspace-root", str(ws_root),
            "--no-checkout",
        ])
        assert result.exit_code == 0
        assert "test-inst-1" in result.output
        assert (ws_root / "test-r1" / "manifest.json").exists()
```

- [ ] **Step 5: 运行全部测试确认通过**

Run: `pytest tests/test_swebench.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/story_lifecycle/cli/swebench.py src/story_lifecycle/cli/main.py tests/test_swebench.py
git commit -m "feat(swebench): 添加 CLI 命令组 story swebench {prepare,solve,export,run}"
```

---

## Task 8: 添加 filelock 依赖 + Cache 锁

**文件：**
- 修改: `pyproject.toml`
- 修改: `src/story_lifecycle/benchmarks/swebench.py`
- 修改: `tests/test_swebench.py`

- [ ] **Step 1: 添加 filelock 到依赖**

在 `pyproject.toml` 的 `dependencies` 中添加 `filelock>=3.0`。

- [ ] **Step 2: 给 _ensure_cache 加进程级文件锁**

修改 `src/story_lifecycle/benchmarks/swebench.py` 中的 `_ensure_cache` 函数：

```python
def _ensure_cache(cache_root: Path, repo: str) -> Path:
    """确保 mirror cache 存在。返回 cache repo 路径。进程级文件锁保护。"""
    from filelock import FileLock

    slug = _repo_slug(repo)
    cache_repo = cache_root / slug
    cache_repo.parent.mkdir(parents=True, exist_ok=True)

    lock_path = str(cache_repo) + ".lock"
    with FileLock(lock_path, timeout=300):
        if cache_repo.exists():
            log.info("Updating cache for %s", repo)
            r = _run_git("remote", "update", "--prune", cwd=str(cache_repo))
            if r.returncode != 0:
                log.warning("Cache update failed for %s: %s", repo, r.stderr)
        else:
            log.info("Creating mirror cache for %s", repo)
            repo_url = f"https://github.com/{repo}.git"
            r = _run_git("clone", "--mirror", repo_url, str(cache_repo))
            if r.returncode != 0:
                raise RuntimeError(f"Mirror clone failed for {repo}: {r.stderr}")

    return cache_repo
```

- [ ] **Step 3: 运行全部测试确认通过**

Run: `pytest tests/test_swebench.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml src/story_lifecycle/benchmarks/swebench.py
git commit -m "feat(swebench): 添加 filelock 依赖和 cache 进程级文件锁"
```

---

## Task 9: 全量 E2E Smoke Test

**文件：**
- 修改: `tests/test_swebench.py`

- [ ] **Step 1: 编写 E2E smoke 测试**

追加到 `tests/test_swebench.py`：

```python
class TestE2ESmoke:
    def test_full_run_no_checkout(self, tmp_path, isolated_story_home):
        """完整流程: prepare(--no-checkout) → export → summarize，不启动 solve。"""
        from story_lifecycle.db import models as db

        # 创建 test JSONL
        instances = tmp_path / "test.jsonl"
        instances.write_text("\n".join([
            json.dumps({
                "instance_id": f"test-{i}", "repo": "a/b",
                "base_commit": f"c{i}", "problem_statement": f"problem {i}",
            })
            for i in range(3)
        ]) + "\n", encoding="utf-8")

        ws_root = tmp_path / "runs"
        runner = CliRunner()

        # Run
        result = runner.invoke(cli, [
            "swebench", "run",
            "--instances", str(instances),
            "--run-id", "e2e-1",
            "--workspace-root", str(ws_root),
            "--no-start",
            "--no-checkout",
        ])
        assert result.exit_code == 0

        # 验证 manifest
        manifest = json.loads((ws_root / "e2e-1" / "manifest.json").read_text(encoding="utf-8"))
        assert len(manifest["instances"]) == 3

        # 验证 Story 创建
        for i in range(3):
            story = db.get_story(f"test-{i}")
            assert story is not None
            assert story["profile"] == "swebench"

        # 验证 predictions.jsonl
        pred_path = ws_root / "e2e-1" / "predictions.jsonl"
        assert pred_path.exists()
        lines = pred_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        # 验证 summary.json
        summary = json.loads((ws_root / "e2e-1" / "summary.json").read_text(encoding="utf-8"))
        assert summary["total"] == 3
        assert summary["predictions"] == 3
```

- [ ] **Step 2: 运行 E2E 测试**

Run: `pytest tests/test_swebench.py::TestE2ESmoke -v`
Expected: PASS

- [ ] **Step 3: 运行全量测试**

Run: `pytest tests/test_swebench.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_swebench.py
git commit -m "test(swebench): 添加 E2E smoke 测试"
```
