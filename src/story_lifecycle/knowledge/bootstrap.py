"""Render bootstrap prompt and run CLI headless for knowledge generation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import knowledge_done_file


def _get_git_commit(workspace: str | Path) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(workspace),
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"


def render_bootstrap_prompt(
    workspace: str | Path,
    scan_profile: str = "java-spring-microservice",
) -> str:
    """Render the knowledge bootstrap prompt with project context."""
    template = _load_prompt_template()
    graph_schema = _load_graph_schema()

    # Use string replacement to avoid .format() issues with JSON braces
    result = template.replace("{graph_schema}", graph_schema)
    result = result.replace("{workspace}", str(workspace))
    result = result.replace("{git_commit}", _get_git_commit(workspace))
    result = result.replace("{scan_profile}", scan_profile)
    return result


def _load_graph_schema() -> str:
    from .templates import load_template

    return load_template("graph-schema.json")


def _load_prompt_template() -> str:
    """Load the bootstrap prompt template."""
    import importlib.resources as _ir

    # Try package prompts/ directory
    try:
        ref = _ir.files("story_lifecycle.prompts").joinpath("knowledge-bootstrap.md")
        return ref.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        pass

    # Fallback: file path relative to package
    pkg = Path(__file__).resolve().parent.parent
    path = pkg / "prompts" / "knowledge-bootstrap.md"
    if path.exists():
        return path.read_text(encoding="utf-8")

    raise FileNotFoundError("knowledge-bootstrap.md prompt template not found")


def run_bootstrap(
    workspace: str | Path,
    scan_profile: str = "java-spring-microservice",
    adapter_name: str = "claude",
    timeout: int = 1800,
) -> dict:
    """Run knowledge bootstrap via CLI headless.

    1. Render prompt
    2. Launch AI CLI in headless mode
    3. Wait for done file (up to timeout seconds)
    4. Return parsed done JSON
    """
    from ..adapters import get_adapter

    workspace = Path(workspace)
    prompt = render_bootstrap_prompt(workspace, scan_profile)

    adapter = get_adapter(adapter_name)
    cmd = adapter.headless_launch_cmd(model="sonnet", prompt=prompt)
    if cmd is None:
        raise RuntimeError(f"Adapter '{adapter_name}' does not support headless mode")

    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(workspace),
        timeout=timeout,
    )

    done = knowledge_done_file(workspace)
    if done.exists():
        return _parse_done(done)

    # Fallback: try to parse JSON from stdout
    import tempfile

    from ..orchestrator.nodes.json_helpers import robust_json_parse

    if proc.stdout.strip():
        # Write stdout to temp file so robust_json_parse can handle it
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(proc.stdout)
            tmp_path = Path(tmp.name)
        try:
            parsed = robust_json_parse(tmp_path)
            if parsed:
                return parsed
        finally:
            tmp_path.unlink(missing_ok=True)

    raise FileNotFoundError(
        f"Bootstrap done file not found at {done}. "
        f"CLI exit code: {proc.returncode}. "
        f"stdout (first 500 chars): {proc.stdout[:500]}"
    )


def _parse_done(path: Path) -> dict:
    from ..orchestrator.nodes.json_helpers import robust_json_parse

    return robust_json_parse(path)


def launch_interactive(
    workspace: str | Path,
    scan_profile: str = "java-spring-microservice",
    adapter_name: str = "claude",
) -> None:
    """Launch interactive AI CLI session with bootstrap prompt injected.

    Saves the prompt to .story/knowledge/bootstrap-prompt.md, then:
    - zellij available: starts claude in zellij, injects prompt
    - Windows (no zellij): opens new terminal with claude, copies to clipboard
    - Unix (no zellij): prints instructions
    """
    import shutil
    import sys

    workspace = Path(workspace)
    prompt = render_bootstrap_prompt(workspace, scan_profile)

    # Save prompt to file for reference
    prompt_file = workspace / ".story" / "knowledge" / "bootstrap-prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    has_zellij = shutil.which("zellij") is not None

    if has_zellij:
        _launch_with_zellij(workspace, prompt, adapter_name)
    elif sys.platform == "win32":
        _launch_windows_terminal(workspace, prompt, adapter_name)
    else:
        _launch_print_instructions(workspace, prompt, adapter_name)


def _launch_with_zellij(workspace: Path, prompt: str, adapter_name: str) -> None:
    """Launch claude in a new zellij session and inject prompt."""
    import time

    session_name = "knowledge-bootstrap"
    cmd = _get_adapter_launch_cmd(adapter_name)

    # Start a new zellij session with claude running in it
    subprocess.Popen(
        ["zellij", "-s", session_name, "--"] + cmd,
        cwd=str(workspace),
    )

    # Wait for claude to start, then inject the instruction
    time.sleep(5)

    inject_text = (
        "请阅读 .story/knowledge/bootstrap-prompt.md 并按照其中的指示生成项目知识包。\n"
    )
    subprocess.run(
        ["zellij", "action", "-s", session_name, "write-chars", inject_text],
        cwd=str(workspace),
        capture_output=True,
    )


def _launch_windows_terminal(workspace: Path, prompt: str, adapter_name: str) -> None:
    """On Windows without zellij: open a new terminal window with claude and auto-paste prompt."""
    cmd = _get_adapter_launch_cmd(adapter_name)
    full_cmd = " ".join(cmd)

    # Copy instruction to clipboard
    inject_text = (
        "请阅读 .story/knowledge/bootstrap-prompt.md 并按照其中的指示生成项目知识包。"
    )
    _copy_to_clipboard(inject_text)

    # Launch claude in a new window, then auto-paste from clipboard after delay
    ps_script = (
        f"$proc = Start-Process cmd -ArgumentList '/k \"{full_cmd}\"' "
        f"-WorkingDirectory '{workspace}' -PassThru; "
        f"Start-Sleep -Seconds 5; "
        f"Add-Type -AssemblyName System.Windows.Forms; "
        f"[System.Windows.Forms.SendKeys]::SendWait('^v'); "
        f"Start-Sleep -Milliseconds 500; "
        f"[System.Windows.Forms.SendKeys]::SendWait('~')"
    )
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
        cwd=str(workspace),
    )


def _launch_print_instructions(workspace: Path, prompt: str, adapter_name: str) -> None:
    """Fallback: print instructions for manual execution."""
    cmd = " ".join(_get_adapter_launch_cmd(adapter_name))
    print("\n提示词已保存到: .story/knowledge/bootstrap-prompt.md")
    print("\n请手动执行:")
    print(f"  cd {workspace}")
    print(f"  {cmd}")
    print("\n然后在 AI CLI 中输入:")
    print(
        "  请阅读 .story/knowledge/bootstrap-prompt.md 并按照其中的指示生成项目知识包。"
    )


def _get_adapter_launch_cmd(adapter_name: str) -> list[str]:
    """Get the interactive launch command for the adapter."""
    from ..adapters import get_adapter

    adapter = get_adapter(adapter_name)
    cmd_str = adapter.launch_cmd(model="sonnet")
    return cmd_str.split() if isinstance(cmd_str, str) else [cmd_str]


def _copy_to_clipboard(text: str) -> None:
    """Copy text to system clipboard."""
    import sys

    try:
        if sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-8"), check=False)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=False,
            )
    except Exception:
        pass
