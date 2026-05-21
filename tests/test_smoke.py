"""Smoke tests — verify package imports and basic CLI registration."""

import importlib


def test_package_imports():
    import story_lifecycle

    assert story_lifecycle is not None


def test_cli_module_imports():
    from story_lifecycle.cli.main import cli

    assert cli is not None
    assert cli.name == "story"


def test_db_module_imports():
    from story_lifecycle.db.models import init_db

    assert callable(init_db)


def test_profiles_load():
    from story_lifecycle.orchestrator.nodes import load_profile

    profile = load_profile("minimal")
    assert "stages" in profile
