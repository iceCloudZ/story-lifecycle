"""Unit tests for diagnostics bundle generation."""

import json
import zipfile
from pathlib import Path
import pytest
from story_lifecycle.orchestrator.diagnostics import (
    create_story_diagnostics_bundle,
    create_global_diagnostics_bundle,
)
from story_lifecycle.db.models import init_db


class TestStoryDiagnosticsBundle:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        """Isolate each test with its own DB and env."""
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        monkeypatch.setenv("STORY_LLM_API_KEY", "fake-key")
        init_db()

    def test_nonexistent_story(self):
        result = create_story_diagnostics_bundle("NONEXISTENT", no_zip=True)
        assert "error" in result

    def test_bundle_structure(self, tmp_path):
        """Bundle contains manifest, summary, debug_packet, and other expected files."""
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-DIAG", "Diag Test", ws)
        (tmp_path / ".story" / "done" / "TEST-DIAG").mkdir(parents=True, exist_ok=True)

        result = create_story_diagnostics_bundle("TEST-DIAG", no_zip=True)
        assert "error" not in result
        bundle_dir = Path(result["path"])
        assert bundle_dir.exists()

        assert (bundle_dir / "manifest.json").exists()
        assert (bundle_dir / "summary.md").exists()
        assert (bundle_dir / "debug_packet.json").exists()
        assert (bundle_dir / "events.jsonl").exists()

        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["bundle_type"] == "story"
        assert manifest["story_key"] == "TEST-DIAG"
        assert len(manifest["files"]) > 0

        summary = (bundle_dir / "summary.md").read_text(encoding="utf-8")
        assert "TEST-DIAG" in summary

    def test_bundle_zip(self, tmp_path):
        """Bundle can be created as a zip file."""
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-ZIP", "Zip Test", ws)
        (tmp_path / ".story" / "done" / "TEST-ZIP").mkdir(parents=True, exist_ok=True)

        result = create_story_diagnostics_bundle("TEST-ZIP", no_zip=False)
        assert "error" not in result
        zip_path = Path(result["path"])
        assert zip_path.suffix == ".zip"
        assert zip_path.exists()

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "summary.md" in names
            assert "debug_packet.json" in names

    def test_summary_includes_stuck_reason(self, tmp_path):
        """summary.md references the stuck_reason code."""
        from story_lifecycle.db.models import create_story

        ws = str(tmp_path)
        create_story("TEST-STUCK", "Stuck Test", ws)
        (tmp_path / ".story" / "done" / "TEST-STUCK").mkdir(parents=True, exist_ok=True)

        result = create_story_diagnostics_bundle("TEST-STUCK", no_zip=True)
        summary = (Path(result["path"]) / "summary.md").read_text(encoding="utf-8")
        assert "卡住原因" in summary


class TestGlobalDiagnosticsBundle:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-lifecycle"))
        monkeypatch.delenv("STORY_LLM_API_KEY", raising=False)
        init_db()

    def test_global_bundle_structure(self):
        """Global bundle works without LLM configured."""
        result = create_global_diagnostics_bundle(no_zip=True)
        assert "error" not in result
        bundle_dir = Path(result["path"])
        assert bundle_dir.exists()
        assert (bundle_dir / "manifest.json").exists()
        assert (bundle_dir / "summary.md").exists()
        assert (bundle_dir / "environment.txt").exists()

        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["bundle_type"] == "global"

    def test_global_bundle_zip(self, tmp_path):
        """Global bundle generates a valid zip."""
        init_db()
        result = create_global_diagnostics_bundle(no_zip=False)
        assert "error" not in result
        zip_path = Path(result["path"])
        assert zip_path.suffix == ".zip"
        assert zip_path.exists()
