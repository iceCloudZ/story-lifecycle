"""LangGraph node implementations — execute, poll, advance, skip, retry, fail."""

import json
import os
import re
import time
from pathlib import Path
from typing import TypedDict, Optional

import yaml

from ..db import models as db
from ..adapters import get_adapter
from ..terminal import ttyd

# Cross-platform file lock
if os.name == 'nt':
    import msvcrt
    def file_lock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
else:
    import fcntl
    def file_lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


TIMEOUT_SECONDS = 30 * 60  # 30 minutes per stage
POLL_INTERVAL = 15  # seconds between poll checks
STORY_HOME = Path.home() / ".story-lifecycle"


class StoryState(TypedDict, total=False):
    story_key: str
    title: str
    workspace: str
    profile: str
    current_stage: str
    status: str
    complexity: str
    context: dict
    execution_count: int
    last_error: Optional[str]
    stage_start_time: float


# -------- stage config --------

def load_profile(profile_name: str) -> dict:
    """Load a profile YAML. Searches: project .story/ → STORY_HOME → built-in."""
    for base in [
        Path.cwd() / ".story",
        STORY_HOME,
        Path(__file__).parent.parent.parent.parent,  # package root (story-lifecycle/)
    ]:
        path = base / "profiles" / f"{profile_name}.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text())
    raise FileNotFoundError(f"Profile not found: {profile_name}")


def get_stage_config(profile_name: str, stage_name: str) -> dict:
    profile = load_profile(profile_name)
    stages = profile.get("stages", {})
    return stages.get(stage_name, {})


def resolve_next_stage(state: StoryState) -> Optional[str]:
    """Determine next stage from profile config + complexity."""
    cfg = get_stage_config(state.get("profile", "minimal"), state["current_stage"])
    next_map = cfg.get("next_default", {})

    if isinstance(next_map, list):
        return next_map[0] if next_map else None
    if isinstance(next_map, dict):
        complexity = state.get("complexity", "M")
        candidates = next_map.get(complexity, next_map.get("default", []))
        return candidates[0] if candidates else None
    return None


# -------- robust JSON parsing --------

def robust_json_parse(filepath: Path) -> dict:
    """Parse .done JSON with tolerance for markdown wrapping."""
    raw = filepath.read_text(encoding="utf-8")

    # Strategy 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: extract first {...} object
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # Strategy 3: try extracting between ```json fences
    m = re.search(r'```json\s*\n(.*?)\n\s*```', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse JSON from {filepath}: {raw[:200]}")


# -------- node: execute_stage --------

def execute_stage_node(state: StoryState) -> StoryState:
    """Launch CC in tmux for the current stage."""
    key = state["story_key"]
    stage = state["current_stage"]
    workspace = state["workspace"]
    profile = state.get("profile", "minimal")

    cfg = get_stage_config(profile, stage)
    adapter_name = cfg.get("cli", load_profile(profile).get("cli", "claude"))
    provider = state.get("context", {}).get("_provider", cfg.get("provider", "deepseek"))
    model = cfg.get("model", "sonnet")
    max_retries = cfg.get("max_retries", 2)

    adapter = get_adapter(adapter_name)

    # 1. Switch provider
    if provider:
        adapter.switch_provider(provider)

    # 2. Ensure ttyd + tmux session (with correct CWD)
    ttyd.ensure_ttyd(key, workspace)

    # 3. Stop existing CC in session
    session = ttyd.session_name(key)
    if ttyd._tmux_session_alive(session):
        ttyd.send_keys(session, "C-c")
        time.sleep(0.5)

    # 4. Create tmux session if not alive (explicit -c for CWD)
    if not ttyd._tmux_session_alive(session):
        import subprocess
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", workspace],
            capture_output=True, timeout=10)
        time.sleep(0.5)

    # 5. Render and inject prompt
    prompt = _render_prompt(stage, state)
    prompt_file = Path(f"/tmp/storypilot-{key}-{stage}.md")
    prompt_file.write_text(prompt, encoding="utf-8")

    # 6. Launch CC
    launch = adapter.launch_cmd(model)
    ttyd.send_keys(session, launch, "Enter")
    time.sleep(8)  # wait for CC to fully initialize

    # 7. Inject prompt via tmux buffer
    buf = f"sp-{key}"
    subprocess.run(["tmux", "load-buffer", "-b", buf, str(prompt_file)], capture_output=True)
    subprocess.run(["tmux", "paste-buffer", "-b", buf, "-t", session], capture_output=True)
    ttyd.send_keys(session, "Enter")

    # 8. Track state
    state["execution_count"] = state.get("execution_count", 0) + 1
    state["stage_start_time"] = time.time()
    state["last_error"] = None

    db.log_stage(key, stage, "execute", f"Attempt {state['execution_count']}")
    db.update_story(key, execution_count=state["execution_count"],
                    last_error=None)

    return state


# -------- node: poll_completion --------

def poll_completion_node(state: StoryState) -> StoryState:
    """Wait for CC to write .story-done/{stage}.json, or crash/timeout."""
    key = state["story_key"]
    stage = state["current_stage"]
    workspace = state["workspace"]
    session = ttyd.session_name(key)
    done_file = Path(workspace) / ".story-done" / f"{stage}.json"
    deadline = state.get("stage_start_time", time.time()) + TIMEOUT_SECONDS

    while time.time() < deadline:
        # Channel 1: .done file exists → success
        if done_file.exists():
            try:
                with open(done_file, "r") as f:
                    file_lock(f)
                    data = robust_json_parse(done_file)
                done_file.unlink()  # clean up after read
                state["context"].update(data)
                # Apply expected_outputs from config
                cfg = get_stage_config(state.get("profile", "minimal"), stage)
                for field in cfg.get("expected_outputs", []):
                    if field in data:
                        db.update_context(key, field, str(data[field]))
                return state
            except Exception as e:
                state["last_error"] = f"Failed to parse .done file: {e}"
                return state

        # Channel 2: tmux session dead → crash
        if not ttyd._tmux_session_alive(session):
            state["last_error"] = "CC process crashed (tmux session dead)"
            return state

        # Channel 3: not ready yet, wait
        time.sleep(POLL_INTERVAL)

    # Timeout
    state["last_error"] = f"Stage timeout after {TIMEOUT_SECONDS}s"
    return state


# -------- node: router --------

def router_node(state: StoryState) -> str:
    """Decide next action. Phase 1: pure if-else, no LLM."""
    if state.get("last_error"):
        # Unhappy path — for now, just fail
        max_retries = get_stage_config(state.get("profile", "minimal"),
                                       state["current_stage"]).get("max_retries", 2)
        if state.get("execution_count", 0) < max_retries:
            return "retry"
        return "fail"

    # Happy path
    cfg = get_stage_config(state.get("profile", "minimal"), state["current_stage"])
    if cfg.get("confirm"):
        return "wait_confirm"
    return "advance"


# -------- node: advance --------

def advance_node(state: StoryState) -> StoryState:
    """Validate expected_outputs, then advance to next stage."""
    key = state["story_key"]
    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)

    # Schema guard: check expected_outputs
    missing = [k for k in cfg.get("expected_outputs", [])
               if k not in state.get("context", {})]
    if missing:
        state["last_error"] = f"Missing expected outputs: {missing}"
        return state  # goes back to router

    next_stage = resolve_next_stage(state)
    if not next_stage:
        db.update_story(key, current_stage=stage, status="completed")
        db.log_stage(key, stage, "complete", "All stages done")
        state["status"] = "completed"
        return state

    db.log_stage(key, stage, "complete", f"Advanced to {next_stage}")
    db.update_story(key, current_stage=next_stage, status="active")

    state["current_stage"] = next_stage
    state["status"] = "active"
    state["execution_count"] = 0
    return state


# -------- node: retry --------

def retry_node(state: StoryState) -> StoryState:
    """Prepare for retry. Clear error, keep count."""
    state["last_error"] = None
    db.log_stage(state["story_key"], state["current_stage"], "retry",
                 f"Retry {state.get('execution_count', 0) + 1}")
    return state


# -------- node: skip --------

def skip_node(state: StoryState) -> StoryState:
    """Skip current stage. Auto-fill expected_outputs with SKIPPED."""
    cfg = get_stage_config(state.get("profile", "minimal"), state["current_stage"])
    for field in cfg.get("expected_outputs", []):
        if field not in state.get("context", {}):
            state["context"][field] = "SKIPPED"
            db.update_context(state["story_key"], field, "SKIPPED")

    db.log_stage(state["story_key"], state["current_stage"], "skip", "Skipped by user")
    db.update_story(state["story_key"], status="active")
    state["status"] = "active"
    state["last_error"] = None
    return state


# -------- node: fail --------

def fail_node(state: StoryState) -> StoryState:
    """Mark story as blocked."""
    db.update_story(state["story_key"], status="blocked",
                    last_error=state.get("last_error", "Unknown error"))
    db.log_stage(state["story_key"], state["current_stage"], "fail",
                 state.get("last_error", "Unknown"))
    state["status"] = "blocked"
    return state


# -------- node: wait_confirm --------

def wait_confirm_node(state: StoryState) -> StoryState:
    """Pause for human confirmation. Advances when status is set back to active."""
    db.update_story(state["story_key"], status="paused")
    db.log_stage(state["story_key"], state["current_stage"], "pause",
                 "Waiting for manual confirmation")
    state["status"] = "paused"
    return state


# -------- prompt rendering --------

def _render_prompt(stage: str, state: StoryState) -> str:
    """Render a prompt for the given stage. Reads built-in templates or falls back to defaults."""
    template_paths = [
        STORY_HOME / "prompts" / f"{stage}.md",
        Path(__file__).parent.parent.parent.parent / "prompts" / f"{stage}.md",
    ]
    template = None
    for p in template_paths:
        if p.exists():
            template = p.read_text(encoding="utf-8")
            break

    if not template:
        # Default prompt
        template = f"""执行阶段: {stage}
Story: {state['story_key']}
标题: {state['title']}

完成后将结果写入项目根目录下的 `.story-done/{stage}.json`。
文件必须只包含纯 JSON，不要用 markdown 代码块包裹。"""

    # Variable substitution
    ctx = state.get("context", {})
    vars_map = {
        "{story_key}": state["story_key"],
        "{title}": state.get("title", ""),
        "{prd_path}": ctx.get("prd_path", ""),
        "{prd_path_section}": (
            f"- PRD 路径: {ctx['prd_path']}\n- 请先读取 PRD 文件：cat {ctx['prd_path']}"
            if ctx.get("prd_path") else ""
        ),
        "{spec_path_section}": (
            f"- Spec 路径: {ctx['spec_path']}" if ctx.get("spec_path") else ""
        ),
    }
    for key, value in vars_map.items():
        template = template.replace(key, value)

    return template
