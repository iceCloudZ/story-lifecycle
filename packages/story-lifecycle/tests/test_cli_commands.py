from click.testing import CliRunner

from story_lifecycle.entry.cli.main import cli


def test_setup_command_is_registered():
    result = CliRunner().invoke(cli, ["setup", "--help"])

    assert result.exit_code == 0
    assert "Configure LLM provider" in result.output


def test_serve_command_is_registered():
    result = CliRunner().invoke(cli, ["serve", "--help"])

    assert result.exit_code == 0
    assert "Start the API server" in result.output


def test_serve_ignores_sighup_to_survive_ssh_disconnect():
    """story serve 必须忽略 SIGHUP,防 ssh 断开/终端关闭突死(2026-08-11 服务器事故)。

    根因:uvicorn 单进程无 SIGHUP-reload 处理器 → ssh 断开发 SIGHUP → 默认 terminate
    → serve 无 traceback 突死。SIG_IGN 让进程自身免疫,无论怎么启动。
    """
    import signal as _sig

    import pytest

    from story_lifecycle.entry.cli.main import _ignore_sighup

    if not hasattr(_sig, "SIGHUP"):
        pytest.skip("SIGHUP not available on this platform (Windows)")
    prior = _sig.getsignal(_sig.SIGHUP)
    try:
        _ignore_sighup()
        assert _sig.getsignal(_sig.SIGHUP) == _sig.SIG_IGN
    finally:
        _sig.signal(_sig.SIGHUP, prior)


def test_doctor_command_is_registered():
    result = CliRunner().invoke(cli, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "System diagnostics" in result.output
