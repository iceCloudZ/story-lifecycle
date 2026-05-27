from click.testing import CliRunner

from story_lifecycle.cli.main import cli


def test_setup_command_is_registered():
    result = CliRunner().invoke(cli, ["setup", "--help"])

    assert result.exit_code == 0
    assert "Configure LLM provider" in result.output


def test_serve_command_is_registered():
    result = CliRunner().invoke(cli, ["serve", "--help"])

    assert result.exit_code == 0
    assert "Start the API server" in result.output


def test_doctor_command_is_registered():
    result = CliRunner().invoke(cli, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "System diagnostics" in result.output
