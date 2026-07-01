import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_zellij_create_session_uses_session_name_as_attach_argument(monkeypatch):
    from story_lifecycle.infra.terminal import ttyd

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr(ttyd, "_MPLEX", "zellij")
    monkeypatch.setattr(ttyd.os, "name", "nt")
    monkeypatch.setattr(ttyd, "_run", fake_run)
    monkeypatch.setattr(ttyd.time, "sleep", lambda _seconds: None)

    ttyd.create_session("s-WIN-ZELLIJ", "/tmp/story-test")

    assert calls[0][0] == [
        "zellij",
        "attach",
        "--create-background",
        "s-WIN-ZELLIJ",
        "options",
        "--default-cwd",
        "/tmp/story-test",
        "--default-shell",
        "powershell.exe",
    ]
    assert calls[0][1]["capture_output"] is False
    assert calls[0][1]["stdin"] is ttyd.subprocess.DEVNULL
    assert calls[0][1]["stdout"] is ttyd.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is ttyd.subprocess.DEVNULL
