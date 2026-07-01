"""Tests for workspace validation — RL-07."""

import stat
import sys
from unittest.mock import patch

import pytest

from story_lifecycle.orchestrator.service.story_service import _validate_workspace, WorkspaceError


class TestWorkspaceValidation:
    def test_valid_directory(self, tmp_path):
        _validate_workspace(str(tmp_path))

    def test_nonexistent_directory(self, tmp_path):
        with pytest.raises(WorkspaceError, match="does not exist"):
            _validate_workspace(str(tmp_path / "nope"))

    def test_file_not_directory(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="not a directory"):
            _validate_workspace(str(f))

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Unix chmod not enforced on Windows"
    )
    def test_no_write_permission(self, tmp_path):
        ws = tmp_path / "readonly"
        ws.mkdir()
        (ws / ".story").mkdir()
        try:
            (ws / ".story").chmod(stat.S_IRUSR | stat.S_IXUSR)
            with pytest.raises(WorkspaceError, match="write permission|Cannot write"):
                _validate_workspace(str(ws))
        finally:
            (ws / ".story").chmod(stat.S_IRWXU)

    def test_write_permission_mocked(self, tmp_path):
        """Simulate write failure via mock (works on all platforms)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".story").mkdir()
        with patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
            with pytest.raises(WorkspaceError, match="write permission|Cannot write"):
                _validate_workspace(str(ws))

    def test_legacy_story_done_warning(self, tmp_path, caplog):
        (tmp_path / ".story-done").mkdir()
        _validate_workspace(str(tmp_path))
        assert any("Legacy" in r.message for r in caplog.records)

    def test_creates_story_dir_on_success(self, tmp_path):
        ws = tmp_path / "new_ws"
        ws.mkdir()
        assert not (ws / ".story").exists()
        _validate_workspace(str(ws))
        assert (ws / ".story").exists()
