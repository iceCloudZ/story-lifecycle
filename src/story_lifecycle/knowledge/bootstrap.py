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
