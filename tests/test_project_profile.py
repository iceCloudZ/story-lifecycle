"""Tests for Workspace Onboarding — project profile, scan, probe, and CLI."""

import json
import subprocess

from click.testing import CliRunner

from story_lifecycle.orchestrator.project_profile import (
    ProjectProfile,
    RepoInfo,
    TestSource,
    Evidence,
    SCHEMA_VERSION,
    profile_path,
    workspace_id,
    _to_dict,
    save_profile,
    load_profile,
    refresh_profile,
)
from story_lifecycle.orchestrator.project_scan import scan_workspace
from story_lifecycle.orchestrator.project_probe import (
    build_probe_prompt,
    validate_probe_output,
    _contains_destructive_pattern,
)
from story_lifecycle.cli.project import project


# ---------------------------------------------------------------------------
# Profile schema tests
# ---------------------------------------------------------------------------


def test_schema_version():
    assert SCHEMA_VERSION == 1


def test_profile_path():
    result = profile_path("/tmp/ws")
    assert str(result).replace("\\", "/").endswith(".story/project/profile.json")


def test_workspace_id_deterministic():
    id1 = workspace_id("/tmp/test-workspace")
    id2 = workspace_id("/tmp/test-workspace")
    assert id1 == id2
    assert len(id1) == 16


def test_workspace_id_normalizes_slashes():
    id_unix = workspace_id("/tmp/test")
    id_win = workspace_id("\\tmp\\test")
    # Both should produce same ID after normalization
    assert id_unix == id_win


def test_project_profile_defaults():
    p = ProjectProfile()
    assert p.schema_version == SCHEMA_VERSION
    assert p.workspace_type == "empty_or_unknown"
    assert p.repos == []
    assert p.test_sources == []
    assert p.confidence == "low"


def test_repo_info_dataclass():
    r = RepoInfo(id="test", name="test", relative_path=".", git_root="/tmp/test")
    assert r.repo_type == "unknown"
    assert r.default_branch == "main"
    assert r.dirty is False


def test_to_dict_round_trip(tmp_path):
    profile = ProjectProfile(
        workspace_root=str(tmp_path),
        workspace_id="abc123",
        workspace_type="single_repo",
        repos=[
            RepoInfo(
                id="my-repo",
                name="my-repo",
                relative_path=".",
                git_root=str(tmp_path),
                repo_type="backend",
                languages=["python"],
                evidence=[Evidence(path=".git", kind="git_dir")],
            )
        ],
        test_sources=[
            TestSource(
                id="my-repo-pytest",
                repo_id="my-repo",
                name="pytest",
                command="pytest",
                evidence=[Evidence(path="pyproject.toml", kind="pyproject.toml")],
            )
        ],
    )

    d = _to_dict(profile)
    assert d["workspace_type"] == "single_repo"
    assert len(d["repos"]) == 1
    assert d["repos"][0]["evidence"][0]["kind"] == "git_dir"


def test_save_and_load_profile(tmp_path):
    profile = ProjectProfile(
        workspace_root=str(tmp_path),
        workspace_id="abc123",
        workspace_type="single_repo",
    )

    saved_path = save_profile(tmp_path, profile)
    assert saved_path.exists()

    loaded = load_profile(tmp_path)
    assert loaded is not None
    assert loaded.workspace_type == "single_repo"
    assert loaded.workspace_id == "abc123"
    assert loaded.schema_version == SCHEMA_VERSION


def test_load_profile_missing(tmp_path):
    assert load_profile(tmp_path) is None


def test_load_profile_corrupt(tmp_path):
    p = profile_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json", encoding="utf-8")
    assert load_profile(tmp_path) is None


# ---------------------------------------------------------------------------
# Scan tests
# ---------------------------------------------------------------------------


def test_scan_single_repo(tmp_path):
    """Single repo: workspace root is a git repo."""
    # Init git repo
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    # Create pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'test'\n", encoding="utf-8"
    )

    profile = scan_workspace(tmp_path)
    assert profile.workspace_type == "single_repo"
    assert len(profile.repos) == 1
    assert profile.repos[0].repo_type == "backend"
    assert "python" in profile.repos[0].languages


def test_scan_multi_repo(tmp_path):
    """Multi repo: workspace contains multiple git repos."""
    for name in ["svc-a", "svc-b"]:
        svc = tmp_path / name
        svc.mkdir()
        subprocess.run(["git", "init"], cwd=str(svc), capture_output=True)
        (svc / "pom.xml").write_text("<project></project>", encoding="utf-8")

    profile = scan_workspace(tmp_path)
    assert profile.workspace_type == "multi_repo"
    assert len(profile.repos) == 2


def test_scan_plain_directory(tmp_path):
    """Plain directory: no git, but has project files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "package.json").write_text('{"name": "test"}', encoding="utf-8")

    profile = scan_workspace(tmp_path)
    assert profile.workspace_type == "plain_directory"


def test_scan_ignores_node_modules(tmp_path):
    """Ignored directories should not appear as repos."""
    nm = tmp_path / "node_modules" / "some-pkg"
    nm.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(nm), capture_output=True)

    # Make root a git repo so it's single_repo
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    profile = scan_workspace(tmp_path)
    assert profile.workspace_type == "single_repo"
    assert len(profile.repos) == 1


def test_scan_test_discovery(tmp_path):
    """Test commands should be discovered from build files."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'test'\n", encoding="utf-8"
    )

    profile = scan_workspace(tmp_path)
    assert any(ts.command == "pytest" for ts in profile.test_sources)


def test_scan_generates_facts(tmp_path):
    """Scan should produce observed facts."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'test'\n", encoding="utf-8"
    )

    profile = scan_workspace(tmp_path)
    assert len(profile.facts) > 0
    fact_types = [f.type for f in profile.facts]
    assert "workspace_type" in fact_types
    assert "repo_count" in fact_types


def test_scan_release_signals(tmp_path):
    """Release profile should detect multi-repo scale."""
    for name in ["backend", "frontend"]:
        svc = tmp_path / name
        svc.mkdir()
        subprocess.run(["git", "init"], cwd=str(svc), capture_output=True)
        if name == "backend":
            (svc / "pom.xml").write_text("<project/>", encoding="utf-8")
        else:
            (svc / "package.json").write_text("{}", encoding="utf-8")

    profile = scan_workspace(tmp_path)
    assert profile.release_profile.scale == "frontend_backend"


def test_default_branch_guess(tmp_path):
    """Default branch should be guessed correctly."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        capture_output=True,
    )

    profile = scan_workspace(tmp_path)
    assert profile.repos[0].default_branch in ("main", "master")


# ---------------------------------------------------------------------------
# Probe validation tests
# ---------------------------------------------------------------------------


def test_destructive_pattern_detection():
    assert _contains_destructive_pattern("rm -rf /")
    assert _contains_destructive_pattern("git reset --hard HEAD~1")
    assert _contains_destructive_pattern("drop table users")
    assert not _contains_destructive_pattern("npm test")
    assert not _contains_destructive_pattern("pytest")


def test_validate_probe_output_valid(tmp_path):
    """Valid probe output should parse correctly."""
    (tmp_path / "README.md").write_text("# Test", encoding="utf-8")
    raw = json.dumps(
        {
            "facts": [
                {
                    "type": "test_command",
                    "value": "pytest",
                    "evidence": [{"path": "README.md", "kind": "readme"}],
                }
            ],
            "hypotheses": [],
            "open_questions": [],
        }
    )

    result = validate_probe_output(raw, tmp_path)
    assert result["valid"] is True
    assert len(result["facts"]) == 1
    assert result["facts"][0]["value"] == "pytest"


def test_validate_probe_output_rejects_no_evidence():
    """Facts without evidence should be rejected."""
    raw = json.dumps(
        {
            "facts": [{"type": "test", "value": "pytest"}],
            "hypotheses": [],
            "open_questions": [],
        }
    )
    result = validate_probe_output(raw, "/tmp")
    assert result["valid"] is True
    assert len(result["facts"]) == 0
    assert len(result["rejected"]) > 0


def test_validate_probe_output_rejects_nonexistent_path(tmp_path):
    """Facts with evidence paths that don't exist on disk should be rejected."""
    raw = json.dumps(
        {
            "facts": [
                {
                    "type": "test",
                    "value": "pytest",
                    "evidence": [{"path": "does_not_exist.txt", "kind": "file"}],
                }
            ],
            "hypotheses": [],
            "open_questions": [],
        }
    )
    result = validate_probe_output(raw, tmp_path)
    assert result["valid"] is True
    assert len(result["facts"]) == 0
    assert any("does not exist" in r["reason"] for r in result["rejected"])


def test_validate_probe_output_rejects_destructive(tmp_path):
    """Facts with destructive commands should be rejected."""
    (tmp_path / "test.txt").write_text("x", encoding="utf-8")
    raw = json.dumps(
        {
            "facts": [
                {
                    "type": "command",
                    "value": "rm -rf /",
                    "evidence": [{"path": "test.txt", "kind": "file"}],
                }
            ],
            "hypotheses": [],
            "open_questions": [],
        }
    )

    result = validate_probe_output(raw, tmp_path)
    assert result["valid"] is True
    assert len(result["facts"]) == 0
    assert any("destructive" in r["reason"] for r in result["rejected"])


def test_validate_probe_output_invalid_json():
    """Invalid JSON should be rejected."""
    result = validate_probe_output("not json at all", "/tmp")
    assert result["valid"] is False
    assert "error" in result


def test_validate_probe_output_markdown_wrapped(tmp_path):
    """Markdown-wrapped JSON should be extracted."""
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    raw = '```json\n{"facts": [], "hypotheses": [], "open_questions": []}\n```'
    result = validate_probe_output(raw, tmp_path)
    assert result["valid"] is True


def test_build_probe_prompt():
    prompt = build_probe_prompt("/tmp/ws", "Find test commands")
    assert "Find test commands" in prompt
    assert "/tmp/ws" in prompt
    assert "read-only" in prompt.lower() or "只读" in prompt


# ---------------------------------------------------------------------------
# Refresh / drift detection tests
# ---------------------------------------------------------------------------


def test_refresh_missing_profile(tmp_path):
    report = refresh_profile(tmp_path)
    assert report.status == "missing_profile"


def test_refresh_no_drift(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    profile = scan_workspace(tmp_path)
    save_profile(tmp_path, profile)

    report = refresh_profile(tmp_path)
    assert report.status == "ok"


def test_refresh_detects_missing_repo(tmp_path):
    # Create a profile with a repo that doesn't exist
    profile = ProjectProfile(
        workspace_root=str(tmp_path),
        repos=[
            RepoInfo(
                id="missing-repo",
                name="missing-repo",
                relative_path="missing-repo",
                git_root=str(tmp_path / "missing-repo"),
            )
        ],
    )
    save_profile(tmp_path, profile)

    report = refresh_profile(tmp_path)
    assert report.status == "drift"
    assert any(d.type == "repo_missing" for d in report.drift)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_inspect_json(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    runner = CliRunner()
    result = runner.invoke(project, ["inspect", "-w", str(tmp_path), "--json"])
    assert result.exit_code == 0

    import re

    clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    start = clean.index("{")
    data = json.loads(clean[start:], strict=False)
    assert data["workspace_type"] == "single_repo"


def test_cli_inspect_human_readable(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    runner = CliRunner()
    result = runner.invoke(project, ["inspect", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert "single_repo" in result.output


def test_cli_onboard_yes(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(project, ["onboard", "-w", str(tmp_path), "--yes"])
    assert result.exit_code == 0

    # Verify profile was saved
    assert load_profile(tmp_path) is not None


def test_cli_onboard_existing(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    # First onboard
    runner = CliRunner()
    runner.invoke(project, ["onboard", "-w", str(tmp_path), "--yes"])

    # Second onboard without --force
    result = runner.invoke(project, ["onboard", "-w", str(tmp_path)])
    assert "already exists" in result.output


def test_cli_onboard_force(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    runner = CliRunner()
    runner.invoke(project, ["onboard", "-w", str(tmp_path), "--yes"])
    result = runner.invoke(
        project, ["onboard", "-w", str(tmp_path), "--yes", "--force"]
    )
    assert result.exit_code == 0


def test_cli_confirm(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    runner = CliRunner()
    runner.invoke(project, ["onboard", "-w", str(tmp_path), "--yes"])

    result = runner.invoke(project, ["confirm", "-w", str(tmp_path)], input="a\n")
    assert result.exit_code == 0

    profile = load_profile(tmp_path)
    assert profile is not None
    assert all(f.confirmed for f in profile.facts)


def test_cli_refresh_ok(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    runner = CliRunner()
    runner.invoke(project, ["onboard", "-w", str(tmp_path), "--yes"])

    result = runner.invoke(project, ["refresh", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert "up-to-date" in result.output


def test_cli_refresh_missing_profile(tmp_path):
    runner = CliRunner()
    result = runner.invoke(project, ["refresh", "-w", str(tmp_path)])
    assert result.exit_code == 1
    assert "No Project Profile" in result.output


def test_cli_probe_no_llm(tmp_path):
    """Probe should fail gracefully without LLM configured."""
    runner = CliRunner()
    env = {"STORY_LLM_API_KEY": ""}
    result = runner.invoke(
        project,
        ["probe", "-w", str(tmp_path)],
        env=env,
    )
    # Should fail with appropriate message
    assert result.exit_code != 0 or "LLM" in result.output
