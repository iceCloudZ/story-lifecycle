"""Contract test: story-lifecycle done_cmd invokes miner retrospect.py.

When a story is marked done, story-lifecycle must call
``python packages/story-miner/scripts/retrospect.py --story <story_key>``.
This test locks the CLI invocation contract.
"""
import os
import subprocess
import sys
from unittest.mock import patch

from story_lifecycle.cli import list_cmd


def test_done_cmd_calls_retrospect_script_with_story_key():
    """done_cmd must invoke the miner retrospect script with --story <key>."""
    with patch.object(list_cmd.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        list_cmd._run_miner_retrospect("STORY-1065518")

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith(os.path.join("packages", "story-miner", "scripts", "retrospect.py"))
    assert "--story" in cmd
    assert "STORY-1065518" in cmd
    assert kwargs.get("capture_output") is True
    assert kwargs.get("timeout") == 120


def test_done_cmd_gracefully_skips_when_script_missing(tmp_path, monkeypatch):
    """If the retrospect script is absent, done_cmd must not crash."""
    monkeypatch.setattr(list_cmd, "_MINER_RETROSPECT_SCRIPT", str(tmp_path / "missing.py"))
    # Should complete without raising
    list_cmd._run_miner_retrospect("STORY-1")
