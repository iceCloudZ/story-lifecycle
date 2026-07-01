"""Regression tests for setup/doctor commands — RL-02."""

import os

import pytest
import yaml
from click.testing import CliRunner

from story_lifecycle.cli.main import cli
from story_lifecycle.cli.setup import (
    is_configured,
)
from story_lifecycle.config import (
    get_config,
    save_config,
    _merge_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_config(tmp_path, monkeypatch):
    """Redirect config dir to a temp directory so tests don't touch ~/.story-lifecycle.

    Patches BOTH the infra `config` module (where get_config/save_config now
    live, post-ISS-006) and `cli.setup` (which re-imports CONFIG_FILE for
    is_configured/`run_setup` display). Without patching `config`, the moved
    get_config/save_config would read/write the real ~/.story-lifecycle.
    """
    cfg_dir = tmp_path / ".story-lifecycle"
    cfg_file = cfg_dir / "config.yaml"
    monkeypatch.setattr("story_lifecycle.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("story_lifecycle.config.CONFIG_FILE", cfg_file)
    monkeypatch.setattr("story_lifecycle.cli.setup.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("story_lifecycle.cli.setup.CONFIG_FILE", cfg_file)
    yield cfg_file


# ---------------------------------------------------------------------------
# is_configured / get_config / save_config
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def test_no_config_file(self, _tmp_config):
        assert is_configured() is False

    def test_empty_config(self, _tmp_config):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text("", encoding="utf-8")
        assert is_configured() is False

    def test_config_without_api_key(self, _tmp_config):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text(yaml.dump({"provider": "deepseek"}), encoding="utf-8")
        assert is_configured() is False

    def test_config_with_api_key(self, _tmp_config):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text(
            yaml.dump({"api_key": "sk-test123", "provider": "deepseek"}),
            encoding="utf-8",
        )
        assert is_configured() is True

    def test_corrupted_yaml(self, _tmp_config):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text("{{{{invalid yaml", encoding="utf-8")
        assert is_configured() is False

    def test_env_var_override(self, _tmp_config, monkeypatch):
        """Env var STORY_LLM_API_KEY should not affect is_configured (checks file only)."""
        monkeypatch.setenv("STORY_LLM_API_KEY", "sk-env")
        assert is_configured() is False


class TestGetConfig:
    def test_no_file_returns_empty(self, _tmp_config):
        assert get_config() == {}

    def test_reads_existing(self, _tmp_config):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        data = {"api_key": "sk-test", "provider": "openai"}
        _tmp_config.write_text(yaml.dump(data), encoding="utf-8")
        assert get_config() == data


class TestSaveConfig:
    def test_creates_dir_and_file(self, _tmp_config):
        save_config({"api_key": "sk-new", "provider": "deepseek", "model": "v4"})
        assert _tmp_config.exists()
        saved = yaml.safe_load(_tmp_config.read_text(encoding="utf-8"))
        assert saved["api_key"] == "sk-new"

    def test_merge_preserves_existing_keys(self, _tmp_config):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text(
            yaml.dump({"api_key": "sk-old", "extra": "kept"}), encoding="utf-8"
        )
        save_config({"api_key": "sk-new"})
        saved = yaml.safe_load(_tmp_config.read_text(encoding="utf-8"))
        assert saved["api_key"] == "sk-new"
        assert saved["extra"] == "kept"


class TestMergeConfig:
    def test_overwrites_keys(self):
        result = _merge_config({"a": 1, "b": 2}, {"b": 3})
        assert result == {"a": 1, "b": 3}

    def test_adds_keys(self):
        result = _merge_config({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_empty_updates(self):
        result = _merge_config({"a": 1}, {})
        assert result == {"a": 1}


# ---------------------------------------------------------------------------
# CLI command behavior
# ---------------------------------------------------------------------------


class TestSetupCLI:
    def test_setup_help(self):
        result = CliRunner().invoke(cli, ["setup", "--help"])
        assert result.exit_code == 0
        assert "Configure LLM provider" in result.output

    def test_setup_bypasses_config_check(self, monkeypatch):
        """`story setup` should run even when not configured."""
        monkeypatch.setattr("story_lifecycle.cli.setup.is_configured", lambda: False)
        result = CliRunner().invoke(cli, ["setup", "--help"])
        assert result.exit_code == 0


class TestDoctorCLI:
    def test_doctor_help(self):
        result = CliRunner().invoke(cli, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "System diagnostics" in result.output

    def test_doctor_bypasses_config_check(self, monkeypatch):
        """`story doctor` should run even when not configured."""
        monkeypatch.setattr("story_lifecycle.cli.setup.is_configured", lambda: False)
        result = CliRunner().invoke(cli, ["doctor", "--help"])
        assert result.exit_code == 0

    def test_doctor_runs_without_config(self):
        """`story doctor` should not crash when no config file exists."""
        result = CliRunner().invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "Doctor" in result.output

    def test_doctor_paths_help(self):
        result = CliRunner().invoke(cli, ["doctor", "paths", "--help"])
        assert result.exit_code == 0

    def test_doctor_paths_runs(self, tmp_path, monkeypatch):
        """`story doctor paths` should scan and report, even with no legacy dirs."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["doctor", "paths"])
        assert result.exit_code == 0
        assert "clean" in result.output.lower() or "legacy" in result.output.lower()


class TestLoadConfigToEnv:
    def test_sets_env_vars(self, _tmp_config, monkeypatch):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text(
            yaml.dump(
                {
                    "api_key": "sk-envtest",
                    "base_url": "https://api.test.com",
                    "model": "test-model",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.delenv("STORY_LLM_API_KEY", raising=False)
        monkeypatch.delenv("STORY_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("STORY_LLM_MODEL", raising=False)

        from story_lifecycle.cli.setup import load_config_to_env

        load_config_to_env()

        assert os.environ.get("STORY_LLM_API_KEY") == "sk-envtest"
        assert os.environ.get("STORY_LLM_BASE_URL") == "https://api.test.com"
        assert os.environ.get("STORY_LLM_MODEL") == "test-model"

    def test_does_not_override_existing_env(self, _tmp_config, monkeypatch):
        _tmp_config.parent.mkdir(parents=True, exist_ok=True)
        _tmp_config.write_text(
            yaml.dump({"api_key": "sk-file"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("STORY_LLM_API_KEY", "sk-preexisting")

        from story_lifecycle.cli.setup import load_config_to_env

        load_config_to_env()

        assert os.environ["STORY_LLM_API_KEY"] == "sk-preexisting"
