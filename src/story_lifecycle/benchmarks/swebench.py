"""SWE-bench benchmark adapter — instance 加载、run 管理、predictions 导出。"""

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..db import models as db


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


def _repo_slug(repo: str) -> str:
    """'owner/name' -> 'owner__name'，用于文件系统路径。"""
    return repo.replace("/", "__")


def _run_git(
    *args: str, cwd: str | None = None, timeout: int = 300
) -> subprocess.CompletedProcess:
    cmd = ["git"] + list(args)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


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
        try:
            cache_repo = _ensure_cache(cache_root, inst.repo)
            cache_available = True
        except RuntimeError as e:
            log.warning("Cache setup 失败，回退到直接 clone: %s", e)
            cache_available = False

        if workspace.exists() and (workspace / ".git").exists():
            r = _run_git("fetch", "origin", cwd=str(workspace))
            if r.returncode != 0:
                return {
                    "status": "checkout_failed",
                    "error": f"fetch failed: {r.stderr}",
                }
            r = _run_git("checkout", inst.base_commit, cwd=str(workspace))
            if r.returncode != 0:
                return {
                    "status": "checkout_failed",
                    "error": f"checkout failed: {r.stderr}",
                }
            _run_git("clean", "-fdx", cwd=str(workspace))
        else:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            if cache_available:
                r = _run_git(
                    "clone",
                    "--reference",
                    str(cache_repo),
                    repo_url,
                    str(workspace),
                )
            else:
                r = _run_git("clone", repo_url, str(workspace))
            if r.returncode != 0:
                return {
                    "status": "checkout_failed",
                    "error": f"clone failed: {r.stderr}",
                }

            r = _run_git("fetch", "origin", inst.base_commit, cwd=str(workspace))
            if r.returncode != 0:
                return {
                    "status": "checkout_failed",
                    "error": f"fetch commit failed: {r.stderr}",
                }
            r = _run_git("checkout", inst.base_commit, cwd=str(workspace))
            if r.returncode != 0:
                return {
                    "status": "checkout_failed",
                    "error": f"checkout failed: {r.stderr}",
                }

        return {"status": "checked_out"}

    except Exception as e:
        return {"status": "checkout_failed", "error": str(e)}


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
    db.update_story(
        inst.instance_id, context_json=json.dumps(context, ensure_ascii=False)
    )
    db.update_story(
        inst.instance_id, source_type="swebench", source_id=inst.instance_id
    )

    return {"status": "prepared", "story_key": inst.instance_id}


def _render_prd(inst: SWEbenchInstance) -> str:
    """渲染 SWE-bench instance 为 PRD markdown。"""
    sections = [
        f"# SWE-bench Instance: {inst.instance_id}",
        "",
        "## Repository",
        "",
        inst.repo,
        "",
        "## Base Commit",
        "",
        inst.base_commit,
        "",
        "## Problem Statement",
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


def export_predictions(
    store: RunStore, run_id: str, agent: str = "claude"
) -> list[dict]:
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

        if noise.get("tags"):
            store.update_instance(run_id, instance_id, noise_tags=noise["tags"])

    pred_path = store.root / run_id / "predictions.jsonl"
    with open(pred_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return rows
