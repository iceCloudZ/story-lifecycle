"""Smoke tests — verify package imports and basic CLI registration."""


def test_package_imports():
    import story_lifecycle

    assert story_lifecycle is not None


def test_main_module_entry():
    """`python -m story_lifecycle` must have a __main__.py."""
    from pathlib import Path

    import story_lifecycle

    pkg = Path(story_lifecycle.__file__).parent
    assert (pkg / "__main__.py").exists(), "__main__.py missing from package"


def test_version_option():
    """CLI --version must return a non-empty string."""
    from click.testing import CliRunner
    from story_lifecycle.cli.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip(), "--version produced empty output"
    assert "unknown" not in result.output.lower(), "--version returned 'unknown'"


def test_cli_module_imports():
    from story_lifecycle.cli.main import cli

    assert cli is not None
    assert cli.name in ("story", "cli")


def test_db_module_imports():
    from story_lifecycle.db.models import init_db

    assert callable(init_db)


def test_profiles_load():
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("minimal")
    assert "stages" in profile


def test_minimal_profile_stages():
    """minimal = design -> implement (2 stages only)."""
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("minimal")
    stages = list(profile["stages"].keys())
    assert stages == ["design", "implement"], f"Expected 2 stages, got {stages}"


def test_strict_profile_stages():
    """strict = design -> review_design -> implement -> review (4 stages)."""
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("strict")
    stages = list(profile["stages"].keys())
    assert stages == ["design", "review_design", "implement", "review"], (
        f"Expected 4 stages, got {stages}"
    )


def test_minimal_profile_no_quality():
    """minimal should not enable quality or adversarial by default."""
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("minimal")
    assert profile.get("quality", {}).get("enabled") is False
    assert profile.get("adversarial", {}).get("enabled") is False


def test_strict_profile_has_quality():
    """strict should enable quality and adversarial."""
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("strict")
    assert profile.get("quality", {}).get("enabled") is True
    assert profile.get("adversarial", {}).get("enabled") is True


def test_packaged_and_root_profiles_consistent():
    """All profiles must be identical between root profiles/ and packaged src/...profiles/."""
    import yaml
    from pathlib import Path

    root_dir = Path(__file__).parent.parent / "profiles"
    pkg_dir = Path(__file__).parent.parent / "src" / "story_lifecycle" / "profiles"

    root_profiles = sorted(p.name for p in root_dir.glob("*.yaml"))
    pkg_profiles = sorted(p.name for p in pkg_dir.glob("*.yaml"))

    assert root_profiles == pkg_profiles, (
        f"Profile lists differ: root={root_profiles}, pkg={pkg_profiles}"
    )

    for name in root_profiles:
        root_yaml = yaml.safe_load((root_dir / name).read_text(encoding="utf-8"))
        pkg_yaml = yaml.safe_load((pkg_dir / name).read_text(encoding="utf-8"))
        assert root_yaml == pkg_yaml, (
            f"Profile '{name}' differs between root/ and src/...profiles/"
        )


def test_service_imports():
    from story_lifecycle.orchestrator.service import create_and_start_story

    assert callable(create_and_start_story)


def test_upsert_story():
    from story_lifecycle.db.models import init_db, upsert_story, get_story

    init_db()
    upsert_story("SMOKE-001", title="Smoke test", workspace="/tmp", status="active")
    s = get_story("SMOKE-001")
    assert s is not None
    assert s["story_key"] == "SMOKE-001"
