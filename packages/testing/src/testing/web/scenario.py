"""Scenario orchestration: drive one story end-to-end through the SPA via WebBridge.

Design discipline — this module only *drives*, it never *judges*:
    * The non-deterministic part is the AI inside story-lifecycle (real Claude
      CLI running design/implement/verify) plus the browser round-trips.
    * Pass/fail is decided later by ``testing.web.judge`` over backend artifacts.
    * If a step here fails to reach the expected backend state, it raises a
      :class:`ScenarioError` so the test layer can distinguish "the UI-driving
      harness couldn't advance the story" from "the story ran but produced bad
      artifacts" (an assertion failure in the judge).

Two driving channels run side by side, both over the real network:
    1. ``StoryApiClient`` (httpx)        — deterministic seeding + status probe.
    2. ``WebBridgeClient`` (real Chrome) — the human-perspective path: see the
       plan card, click "确认规划", click stage-gate / lifecycle-gate buttons,
       answer clarify dialogs. The SPA's dynamic button labels are matched by
       prefix (see webbridge.find_refs).

The default scenario is the calculator (red→green) — the same one the existing
in-process ``tests/e2e/test_calculator_real_e2e.py`` covers, now exercised over
HTTP + browser so the WebSocket/SSE/SPA paths are actually tested.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from testing.harness import StoryRunResult, StageResult, safe_segment
from testing.web.api_client import StoryApiClient
from testing.web.server import RunningServer
from testing.web.webbridge import WebBridgeClient, WebBridgeError
from testing.workspace import reset_workspace

log = logging.getLogger("testing.web.scenario")

# Event trace — write to a fixed file (bypasses logging buffers + terminal
# clear-screen pollution) so the scenario's actual step sequence is observable
# even when pytest -s output gets eaten. Set WEBBRIDGE_TRACE to enable.
import os as _os
import time as _time

_TRACE_PATH = _os.environ.get("WEBBRIDGE_TRACE", "")


def _trace(msg: str) -> None:
    if not _TRACE_PATH:
        return
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


class ScenarioError(RuntimeError):
    """The scenario harness could not drive the story to the expected state.

    Distinct from a judge assertion failure: this means the UI/backend driving
    path broke (button not found, plan never confirmed, stuck in a bad state),
    not that the story produced wrong artifacts.
    """


# ---------------------------------------------------------------------------
# Workspace preparation — how each scenario sets up + tears down its workspace.
# Calculator owns its workspace (PRD + tests + missing impl all live there);
# a real repo (hc-order) needs to INJECT spec/test files and clean them up so
# the user's real working tree is left untouched.
# ---------------------------------------------------------------------------


class WorkspacePrep:
    """Prepare a workspace before the run and clean it up after.

    Subclasses implement :meth:`prepare` (red baseline) and :meth:`cleanup`
    (remove anything the run created/injected). ``workspace`` is where the AI
    operates; ``scenario_dir`` is where the scenario's static assets live.
    """

    def prepare(self, workspace: Path, scenario_dir: Path, story_key: str) -> None:
        raise NotImplementedError

    def cleanup(self, workspace: Path, scenario_dir: Path, story_key: str) -> None:
        raise NotImplementedError


class CalculatorPrep(WorkspacePrep):
    """Red baseline: delete calculator.py + clear .story/{context,done,runs}/<key>.

    Delegates to ``testing.workspace.reset_workspace`` (same as the in-process
    harness). The scenario dir IS the workspace here.
    """

    def prepare(self, workspace: Path, scenario_dir: Path, story_key: str) -> None:
        reset_workspace(workspace, story_key)

    def cleanup(self, workspace: Path, scenario_dir: Path, story_key: str) -> None:
        # reset_workspace is idempotent and safe to re-run; nothing extra needed.
        pass


class InjectedSpecPrep(WorkspacePrep):
    """For real repos: copy spec/test files from scenario_dir into the workspace,
    and remove them (plus any AI-generated impl files) on cleanup.

    Parameters
    ----------
    inject:
        Mapping of ``relative-src-under-scenario_dir`` → ``relative-dst-under-workspace``.
        Each file is copied verbatim before the run (e.g. a JUnit test into
        ``hc-order-business/src/test/java/...``).
    red_files:
        Relative paths under workspace of files the AI is expected to create
        (red = absent). Cleaned up after the run so the real tree is untouched.
    """

    def __init__(
        self,
        inject: dict[str, str],
        red_files: list[str],
    ):
        self.inject = dict(inject)
        self.red_files = list(red_files)

    def prepare(self, workspace: Path, scenario_dir: Path, story_key: str) -> None:
        for src_rel, dst_rel in self.inject.items():
            src = scenario_dir / src_rel
            dst = workspace / dst_rel
            if not src.exists():
                raise ScenarioError(f"injected spec file missing: {src}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            log.info("injected %s → %s", src_rel, dst_rel)
        # Red baseline: make sure none of the AI's target files pre-exist.
        for rel in self.red_files:
            p = workspace / rel
            if p.exists():
                p.unlink()

    def cleanup(self, workspace: Path, scenario_dir: Path, story_key: str) -> None:
        # Only remove the files WE injected + the impl file the AI wrote — NOT
        # the .story/{context,done,runs} artifacts. Those are the judge's
        # evidence (done files, retrospect.md) and must survive cleanup so the
        # judge layer (which runs AFTER run_scenario returns, i.e. AFTER this
        # cleanup) can assert against them. The real tree's git status is still
        # left clean: injected test + AI-written impl are gone; only story-
        # lifecycle's own .story/ bookkeeping remains (acceptable residue, and
        # what a real story run would leave behind too).
        for dst_rel in self.inject.values():
            p = workspace / dst_rel
            if p.exists():
                p.unlink()
        for rel in self.red_files:
            p = workspace / rel
            if p.exists():
                p.unlink()


# SPA label anchors (from frontend exploration — these are stable, the dynamic
# ones are matched by prefix). See plan §"前端语义信息".
_LBL_CONFIRM_PLAN = "✅ 确认规划，开始执行"  # OverviewTab.tsx:248
_LBL_CLARIFY_HEADER = "设计澄清 · 需要你拍板"  # ClarifyDialog.tsx:69
_LBL_CLARIFY_SEND = "送出"  # ClarifyDialog.tsx:103
_PREFIX_STAGE_GATE = "确认推进"  # OverviewTab.tsx:181 (确认推进 → {stage})
_PREFIX_STATE_GATE = "进入"  # OverviewTab.tsx:158 (进入 {state} →)
_LBL_RESUME = "继续执行"  # OverviewTab.tsx ACTIONS[paused] (执行中断恢复, PUT /advance)


@dataclass
class ScenarioResult:
    """Outcome of a web-driven scenario run (mirrors harness.StoryRunResult)."""

    story_key: str
    workspace: str
    server: RunningServer
    plan_events: list[dict] = field(default_factory=list)
    stage_gate_clicks: list[str] = field(default_factory=list)
    clarify_answers: list[dict] = field(default_factory=list)
    final_status: str | None = None
    error: str | None = None

    def to_run_result(self, stages: list[str]) -> StoryRunResult:
        """Adapt to harness.StoryRunResult so the existing asserters/judges apply."""
        result = StoryRunResult(story_key=self.story_key, workspace=self.workspace)
        result.final_story = {"status": self.final_status}
        for stage in stages:
            done = (
                Path(self.workspace)
                / ".story"
                / "done"
                / safe_segment(self.story_key)
                / f"{stage}.json"
            )
            result.stages.append(
                StageResult(
                    stage=stage,
                    story_key=self.story_key,
                    done_file=done,
                    story_snapshot={"status": self.final_status},
                    error=self.error,
                )
            )
        return result


def run_scenario(
    *,
    server: RunningServer,
    webbridge: WebBridgeClient,
    workspace: str | Path,
    scenario_dir: str | Path,
    story_key: str,
    prd_content: str,
    prep: WorkspacePrep,
    stages: list[str] | None = None,
    title: str = "WebBridge E2E story",
    profile: str = "minimal",
    use_browser_for_gates: bool = True,
    gate_timeout: float = 1800.0,
    poll_interval: float = 3.0,
    ui_seed: bool = False,
    ui_workspace_path: str | None = None,
    ui_project_name: str | None = None,
) -> ScenarioResult:
    """Drive one story end-to-end over the real HTTP + browser surface.

    UI vs API seeding
    -----------------
    ``ui_seed=False`` (default): story is created via API (``POST /api/story`` +
    ``/start``). Faster, isolated-DB friendly — used by the calculator scenario.

    ``ui_seed=True``: story is created entirely through the SPA's IntakeStartModal
    (click "新建并开始" → fill key/title → pick workspace → pick project → fill
    PRD → click "准备 PRD 并进入规划"). NO API fallback for any user action —
    a missing button is a :class:`ScenarioError`, not a silent API detour. This
    is the "real human path" mode. Requires ``real_webbridge_server`` (a
    registered workspace exists in the DB) and:

      * ``ui_workspace_path``: the workspace path to pick in the dropdown
        (e.g. ``D:\\hc-all``).
      * ``ui_project_name``: the project to check in the affected-projects list
        (e.g. ``hc-config``).

    Read-only API calls (status polling, get_pending_clarify) are still used to
    *decide which button to click next* — they don't change story state, so they
    don't count as "skipping the UI".

    Generic over workspace preparation: pass a :class:`WorkspacePrep` that knows
    how to set up (red baseline) and tear down (clean the real tree) for the
    target repo. ``workspace`` is where the AI operates; ``scenario_dir`` holds
    static scenario assets (PRD, injected spec/test files).

    Steps
    -----
    1. prep.prepare(workspace, scenario_dir) → red baseline + inject specs.
    2. StoryApiClient.create_story + start(PRD) — deterministic seeding over HTTP.
    3. plan_stream (SSE) — observe the real LLM planner emit actions.
    4. confirm_plan — begin execution; real Claude CLI launches per stage.
    5. Poll backend status:
       - on ``awaiting-clarify`` → answer via API + (optionally) the SPA dialog;
       - on ``paused`` (stage gate / state gate) → click the matching SPA button
         via WebBridge, then fall back to the API gate if the UI click didn't land;
       - until ``completed`` / terminal.
    6. Return ScenarioResult (adapt to StoryRunResult for the judge).
    7. prep.cleanup(workspace, scenario_dir) in a finally — never leave the real
       tree dirty even if the drive failed.

    ``use_browser_for_gates`` keeps the WebBridge path on the critical path; flip
    to False to drive gates purely via API (faster, for triage).
    """
    stages = list(stages or ["design", "implement", "verify"])
    workspace = Path(workspace).resolve()
    scenario_dir = Path(scenario_dir).resolve()
    api = StoryApiClient(server.base_url)
    result = ScenarioResult(
        story_key=story_key, workspace=str(workspace), server=server
    )
    # UI mode is all-or-nothing for user actions: if we seed via UI we also drive
    # every gate via UI. No API fallback for any state-changing operation.
    use_browser = use_browser_for_gates or ui_seed

    try:
        prep.prepare(workspace, scenario_dir, story_key)
        if ui_seed:
            _seed_via_ui(
                webbridge,
                server,
                story_key=story_key,
                title=title,
                profile=profile,
                prd_content=prd_content,
                workspace_path=ui_workspace_path or str(workspace),
                project_name=ui_project_name or "",
                result=result,
            )
        else:
            _seed(api, workspace, story_key, title, profile, prd_content, result)
        _run_plan(api, story_key, result)
        _confirm_plan(api, webbridge, story_key, use_browser, ui_seed, result)
        _drive_to_completion(
            api, webbridge, story_key, use_browser, ui_seed,
            gate_timeout, poll_interval, result,
        )
        result.final_status = api.story_status(story_key)
    except ScenarioError:
        result.error = "scenario drive failed"
        try:
            result.final_status = api.story_status(story_key)
        except Exception:
            pass
        raise
    except Exception as exc:  # unexpected → wrap so the judge layer sees a clean signal
        result.error = f"{type(exc).__name__}: {exc}"
        try:
            result.final_status = api.story_status(story_key)
        except Exception:
            pass
        raise ScenarioError(str(exc)) from exc
    finally:
        try:
            prep.cleanup(workspace, scenario_dir, story_key)
        except Exception as exc:
            log.warning("workspace cleanup failed (real tree may have residue): %s", exc)
    return result


def run_calculator_scenario(
    *,
    server: RunningServer,
    webbridge: WebBridgeClient,
    scenario_dir: str | Path,
    story_key: str,
    prd_content: str,
    stages: list[str] | None = None,
    title: str = "WebBridge E2E story",
    profile: str = "minimal",
    use_browser_for_gates: bool = True,
    gate_timeout: float = 1800.0,
    poll_interval: float = 3.0,
) -> ScenarioResult:
    """Calculator red→green — thin wrapper over :func:`run_scenario`.

    The scenario dir IS the workspace (calculator owns its PRD/tests/impl).
    """
    return run_scenario(
        server=server,
        webbridge=webbridge,
        workspace=scenario_dir,
        scenario_dir=scenario_dir,
        story_key=story_key,
        prd_content=prd_content,
        prep=CalculatorPrep(),
        stages=stages,
        title=title,
        profile=profile,
        use_browser_for_gates=use_browser_for_gates,
        gate_timeout=gate_timeout,
        poll_interval=poll_interval,
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _seed(
    api: StoryApiClient,
    workspace: Path,
    story_key: str,
    title: str,
    profile: str,
    prd_content: str,
    result: ScenarioResult,
) -> None:
    api.create_story(
        story_key,
        title=title,
        workspace=str(workspace),
        profile=profile,
    )
    api.start(story_key, content=prd_content)
    log.info("seeded story %s in %s", story_key, workspace)


# SPA label anchors for the IntakeStartModal (Dashboard.tsx:240-631).
_LBL_NEW_STORY = "新建并开始"  # Dashboard.tsx:144
_LBL_READ_REQ = "读取需求"  # Dashboard.tsx:433 (optional)
_LBL_START_PLANNING = "准备 PRD 并进入规划"  # Dashboard.tsx:625


def _click_with_retry(
    webbridge: WebBridgeClient,
    *,
    exact: str | None = None,
    prefix: str | None = None,
    contains: str | None = None,
    role: str | None = None,
    attempts: int = 10,
    interval: float = 1.5,
    desc: str = "element",
) -> str:
    """Snapshot + click a matching element, retrying while the SPA renders.

    UI-driven mode never falls back to the API: if the element never appears,
    raise :class:`ScenarioError`. Retries absorb React mount/query-resolve lag
    and async status flips, NOT missing features.
    """
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            return webbridge.click_text(
                exact=exact, prefix=prefix, contains=contains, role=role
            )
        except WebBridgeError as exc:
            last_err = exc
            time.sleep(interval)
    raise ScenarioError(
        f"UI element never appeared after {attempts} tries ({desc}): {last_err}"
    )


def _fill_with_retry(
    webbridge: WebBridgeClient,
    value: str,
    *,
    exact: str | None = None,
    contains: str | None = None,
    attempts: int = 10,
    interval: float = 1.0,
    desc: str = "input",
    is_textarea: bool = False,
) -> str:
    """Snapshot + fill a matching input, retrying while it renders.

    ``is_textarea=True``: use ``evaluate`` with chunked React-native value
    setting. The WebBridge ``fill`` tool throws ``Uncaught`` on this controlled
    textarea regardless of content (extension bug), so eval is the only path;
    chunking keeps each eval short enough to avoid daemon truncation on SPA pages.
    """
    last_err: Exception | None = None
    for _ in range(attempts):
        if is_textarea:
            try:
                return _fill_textarea_via_eval(webbridge, value, contains=contains)
            except WebBridgeError as exc:
                last_err = exc
                time.sleep(interval)
                continue
        try:
            return webbridge.fill_text(value, exact=exact, contains=contains)
        except WebBridgeError as exc:
            last_err = exc
            time.sleep(interval)
    raise ScenarioError(
        f"UI input never appeared after {attempts} tries ({desc}): {last_err}"
    )


def _fill_textarea_via_eval(
    webbridge: WebBridgeClient, value: str, *, contains: str | None = None
) -> str:
    """Set a <textarea>'s value via the native value setter (React-safe).

    The WebBridge ``fill`` tool throws ``Uncaught`` on this controlled textarea
    (confirmed for ALL content — it's an extension bug for this element, not a
    content issue). So we must use ``evaluate``. But long evaluate bodies get
    truncated by the daemon on SPA pages ("Unexpected end of input").

    Resolution: chunk the value into small pieces and append each via a SHORT
    evaluate that uses React's native value setter + input event. Each chunk's
    code stays well under the truncation threshold.
    """
    import json as _json

    contains_js = _json.dumps(contains or "")

    def _append(chunk: str) -> None:
        chunk_js = _json.dumps(chunk)
        # Short code: find textarea by placeholder substring, append chunk via
        # native setter, dispatch input so React sees the update.
        code = (
            "(()=>{"
            "var ts=document.querySelectorAll('textarea');"
            "var t=null;"
            f"var sub={contains_js};"
            "for(var k=0;k<ts.length;k++){if((ts[k].placeholder||'').indexOf(sub)>=0){t=ts[k];break}}"
            "if(!t)t=ts[0];"
            "if(!t)return {ok:0};"
            "var g=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
            "g.call(t,t.value+" + chunk_js + ");"
            "t.dispatchEvent(new Event('input',{bubbles:true}));"
            "return {ok:1,len:t.value.length}"
            "})()"
        )
        res = webbridge.evaluate(code)
        payload = res.get("value") or {}
        if not payload.get("ok"):
            raise WebBridgeError(f"textarea chunk append failed: {payload}")

    # First chunk: clear-and-set (append to empty resets). Use small chunks so
    # each evaluate code stays short.
    chunk_size = 120
    for i in range(0, len(value), chunk_size):
        _append(value[i : i + chunk_size])
    return "@textarea"


def _seed_via_ui(
    webbridge: WebBridgeClient,
    server: RunningServer,
    *,
    story_key: str,
    title: str,
    profile: str,
    prd_content: str,
    workspace_path: str,
    project_name: str,
    result: ScenarioResult,
) -> None:
    """Create the story entirely through the SPA's IntakeStartModal.

    Real human path: dashboard → "新建并开始" → fill key/title → pick workspace
    → pick project → fill PRD → "准备 PRD 并进入规划". NO API detour; any missing
    UI element raises :class:`ScenarioError`.

    Workspace/project selection uses the dropdown <option> values: workspaces are
    keyed by path, projects by name. We drive <select> via JS ``evaluate`` because
    click_text can't reach <option> elements reliably across browsers.
    """
    webbridge.navigate(f"{server.base_url}/", group_title=f"e2e {story_key}")
    time.sleep(2.0)  # let the dashboard + workspaces query resolve
    _click_with_retry(webbridge, exact=_LBL_NEW_STORY, role="button", desc="新建并开始")

    # Story key + title (text inputs).
    _fill_with_retry(
        webbridge, story_key, contains="TAPD Story ID", desc="story key input"
    )
    _fill_with_retry(webbridge, title, contains="标题", desc="title input")

    # Profile <select> — pick by option value via JS (options are profile names).
    _select_option_by_value(webbridge, "profile", profile, desc="profile select")

    # Workspace <select> — options are workspace paths.
    _select_option_by_value(
        webbridge, "workspace", workspace_path, desc="workspace select"
    )
    time.sleep(1.0)  # workspace change loads its project list

    # Affected project — checkbox labelled by project name.
    if project_name:
        _click_with_retry(
            webbridge, contains=project_name, role="checkbox", desc=f"project {project_name}"
        )

    # PRD textarea (the only <textarea> on the modal). Use eval-based fill —
    # the WebBridge fill tool throws Uncaught on this controlled component.
    _fill_with_retry(
        webbridge, prd_content, contains="粘贴需求", desc="PRD textarea", is_textarea=True
    )

    _click_with_retry(
        webbridge,
        exact=_LBL_START_PLANNING,
        role="button",
        attempts=15,
        desc="准备 PRD 并进入规划",
    )
    _trace(f"SEED clicked 提交 button for {story_key}")
    time.sleep(3)  # let the POST /api/story/{key}/start + navigate settle
    _trace(f"SEED done for {story_key}")
    log.info("seeded story %s via SPA IntakeStartModal", story_key)


def _select_option_by_value(
    webbridge: WebBridgeClient,
    field_label_substr: str,
    value: str,
    *,
    desc: str = "select",
) -> None:
    """Set a <select> to the option whose value OR text contains ``value``.

    Selects the right <select> by finding the one that HAS a matching option
    (not by label text — label matching was fragile across React re-renders and
    led to picking the wrong select). Drives it via ``evaluate`` setting
    ``.value`` + change event (React controlled-select update).

    ``field_label_substr`` is kept for diagnostics only.
    """
    import json as _json

    value_js = _json.dumps(value)
    # Keep the code SHORT — long evaluate bodies get truncated on some SPA pages
    # by the WebBridge daemon ("Unexpected end of input"). No f-string (its `{{`
    # escaping conflicts with JS braces) — plain concatenation instead.
    code = (
        "(()=>{"
        "var ss=document.querySelectorAll('select');"
        "for(var i=0;i<ss.length;i++){"
        "var s=ss[i];"
        "for(var j=0;j<s.options.length;j++){"
        "var o=s.options[j];"
        "if((o.value||'').indexOf(" + value_js + ")>=0||(o.textContent||'').indexOf(" + value_js + ")>=0){"
        "s.value=o.value;s.dispatchEvent(new Event('change',{bubbles:true}));return {ok:1}"
        "}"
        "}"
        "}"
        "return {ok:0}"
        "})()"
    )
    # Retry: the <option> list is populated asynchronously (Dashboard fetches
    # /api/workspaces on mount, and again on modal open). Selection before the
    # options render sees only the placeholder — wait and retry.
    last_res = None
    for _ in range(15):
        res = webbridge.evaluate(code)
        payload = res.get("value") or {}
        if payload.get("ok"):
            return
        last_res = payload
        if payload.get("reason") == "no select":
            break  # select itself missing — no point retrying
        time.sleep(1.0)
    raise ScenarioError(
        f"UI select '{desc}' (label~{field_label_substr!r}) could not pick "
        f"{value!r} after retries: {last_res}"
    )


def _run_plan(api: StoryApiClient, story_key: str, result: ScenarioResult) -> None:
    events = api.plan_stream(story_key, timeout=120.0)
    result.plan_events = events
    if not any(e.get("type") == "done" for e in events):
        # Some flows plan lazily; if stream errored or produced nothing, surface it.
        err = next((e for e in events if e.get("type") == "error"), None)
        if err:
            raise ScenarioError(f"plan generation failed: {err}")
    log.info("planned %s: %d events", story_key, len(events))


def _confirm_plan(
    api: StoryApiClient,
    webbridge: WebBridgeClient,
    story_key: str,
    use_browser: bool,
    strict_ui: bool,
    result: ScenarioResult,
) -> None:
    """Confirm the plan. SPA path when use_browser; API only as non-strict fallback."""
    if use_browser:
        try:
            webbridge.navigate(f"{result.server.base_url}/story/{story_key}")
            _trace(f"CONFIRM navigated to detail for {story_key}")
            _click_with_retry(
                webbridge,
                exact=_LBL_CONFIRM_PLAN,
                role="button",
                attempts=20,
                interval=2.0,
                desc="✅ 确认规划，开始执行",
            )
            _trace(f"CONFIRM clicked 确认规划 for {story_key}")
            log.info("confirmed plan via SPA for %s", story_key)
            return
        except ScenarioError:
            if strict_ui:
                raise  # UI mode: no API fallback, surface the failure
            log.warning("SPA plan-confirm failed; falling back to API")
    if strict_ui:
        raise ScenarioError("strict_ui set but browser path not taken")
    api.confirm_plan(story_key)
    log.info("confirmed plan via API for %s", story_key)


def _drive_to_completion(
    api: StoryApiClient,
    webbridge: WebBridgeClient,
    story_key: str,
    use_browser: bool,
    strict_ui: bool,
    timeout: float,
    interval: float,
    result: ScenarioResult,
) -> None:
    """Advance gates (stage / lifecycle / clarify) until a terminal status."""
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    stuck_since: float | None = None

    while time.monotonic() < deadline:
        status = api.story_status(story_key)
        if status != last_status:
            log.info("story %s status: %s", story_key, status)
            _trace(f"DRIVE status={status} stage={api.story(story_key).get('currentStage')}")
            last_status = status
            stuck_since = None

        if status in {"completed", "failed", "aborted", "archived"}:
            return

        # 1) Clarify gate has its own status; answer it.
        if _answer_one_clarify(api, webbridge, story_key, use_browser, strict_ui, result):
            continue

        # 2) Paused → a stage or lifecycle gate is awaiting confirmation.
        if status == "paused":
            if _advance_gate(api, webbridge, story_key, use_browser, strict_ui, result):
                continue
            # gate existed in DB but no clickable button yet; brief wait

        # 3) Stuck detection: a NON-active status that doesn't change for a long
        # time. "active"/"implementing" are EXCLUDED — a stage running real AI
        # (Claude writing code) legitimately stays active for many minutes, so
        # timing out on active would false-positive on slow-but-healthy runs.
        # Only paused/blocked/planning that won't advance counts as stuck.
        if status in {"active", "implementing"}:
            stuck_since = None  # actively executing — not stuck
        elif stuck_since is None:
            stuck_since = time.monotonic()
        elif time.monotonic() - stuck_since > 300:
            raise ScenarioError(
                f"story {story_key} stuck in status={status} for >300s"
            )
        time.sleep(interval)

    raise ScenarioError(
        f"story {story_key} did not reach terminal status within {timeout}s "
        f"(last status={last_status})"
    )


def _answer_one_clarify(
    api: StoryApiClient,
    webbridge: WebBridgeClient,
    story_key: str,
    use_browser: bool,
    strict_ui: bool,
    result: ScenarioResult,
) -> bool:
    """If a clarify question is pending, answer it via the SPA. Returns True if answered.

    The pending question is read via API (read-only — just to know the options),
    but the ANSWER is submitted by clicking the SPA dialog. In strict_ui mode a
    failed SPA click is a :class:`ScenarioError` — no API submission fallback.
    """
    try:
        data = api.get_pending_clarify(story_key)
    except Exception:
        return False
    if not data.get("waiting"):
        return False
    q = data.get("question") or {}
    qid = q.get("id")
    options = q.get("options") or []
    answer = options[0] if options else "继续"

    if not use_browser:
        # non-strict path: submit via API
        api.answer_clarify(story_key, answer, qid=qid)
        result.clarify_answers.append({"id": qid, "question": q.get("question"), "answer": answer})
        return True

    # UI path: click the option button (or fill + 送出 for free-text).
    try:
        if options:
            webbridge.click_text(exact=answer, role="button")
        else:
            webbridge.fill_text(answer, contains="自定义回答")
            webbridge.click_text(exact=_LBL_CLARIFY_SEND, role="button")
    except WebBridgeError as exc:
        if strict_ui:
            raise ScenarioError(
                f"clarify dialog present but SPA answer failed (no API fallback): {exc}"
            ) from exc
        log.warning("SPA clarify answer failed; submitting via API")
        api.answer_clarify(story_key, answer, qid=qid)

    result.clarify_answers.append({"id": qid, "question": q.get("question"), "answer": answer})
    log.info("answered clarify %s for %s: %s", qid, story_key, answer)
    return True


def _advance_gate(
    api: StoryApiClient,
    webbridge: WebBridgeClient,
    story_key: str,
    use_browser: bool,
    strict_ui: bool,
    result: ScenarioResult,
) -> bool:
    """Click the right SPA button to advance a paused story. Returns True if advanced.

    A paused story can be in one of TWO states (must distinguish — they show
    different buttons):
      * STAGE GATE paused: ``_stage_gate.awaiting_confirm=True`` (a stage
        completed, awaiting confirm to advance). SPA shows "确认推进 → {next}"
        or "进入 {state} →".
      * EXECUTION paused: ``_stage_gate`` is None/empty (the AI CLI paused mid-
        stage, e.g. a confirm:true stage awaiting a resume, or an interactive
        pause). SPA shows "继续执行".

    We read the backend gate to decide which button to look for, then retry-
    click it (the button renders asynchronously after the status flip). In
    strict_ui mode a missing button is a :class:`ScenarioError` — no API detour.
    """
    if not use_browser:
        return _advance_gate_via_api(api, story_key, result)

    # Read backend to classify the paused state.
    gate = None
    try:
        st = api.story(story_key)
        ctx = st.get("contextJson") if isinstance(st.get("contextJson"), dict) else {}
        gate = ctx.get("_stage_gate")
    except Exception:
        pass
    is_stage_gate = bool(gate and gate.get("awaiting_confirm"))

    # Choose button candidates by paused kind.
    if is_stage_gate:
        # State gate (进入 X →) first, then stage gate (确认推进 → ...).
        candidates = [("prefix", _PREFIX_STATE_GATE), ("prefix", _PREFIX_STAGE_GATE)]
    else:
        # Execution paused → "继续执行" (exact), plus prefix fallbacks just in case.
        candidates = [("exact", _LBL_RESUME), ("prefix", _PREFIX_STATE_GATE), ("prefix", _PREFIX_STAGE_GATE)]

    # Navigate to the detail page so the gate button is actually rendered. The
    # poll loop may have left the page on a stale state (or the gate flipped
    # after the last navigate); a fresh navigate + render wait is what makes
    # the button reliably present (verified in isolation).
    try:
        webbridge.navigate(f"{result.server.base_url}/story/{story_key}")
        time.sleep(3.0)  # let React mount + the planning/gate query resolve
    except WebBridgeError as exc:
        log.warning("navigate before advance failed: %s", exc)

    clicked: str | None = None
    last_err: Exception | None = None
    for kind, val in candidates:
        kw = {"role": "button", "attempts": 8, "interval": 1.5, "desc": f"{kind}={val!r}"}
        if kind == "exact":
            kw["exact"] = val
        else:
            kw["prefix"] = val
        try:
            ref = _click_with_retry(webbridge, **kw)
            clicked = f"a11y {kind}={val!r} ({ref})"
            break
        except ScenarioError as exc:
            last_err = exc
            continue
    # Fallback: a11y snapshot sometimes misses the gate button (it's in a card
    # some snapshot passes don't expose). Try a DOM-level click via evaluate —
    # still a real UI click (button.click()), just located through the DOM tree
    # instead of the a11y tree. This keeps the "no API detour" guarantee.
    if clicked is None:
        for kind, val in candidates:
            dom_kw = {"exact": val} if kind == "exact" else {"contains": val}
            if webbridge.click_dom_button(**dom_kw):
                clicked = f"dom {kind}={val!r}"
                break
    if clicked is None:
        if strict_ui:
            raise ScenarioError(
                f"no SPA advance button (paused, stage_gate={gate}): {last_err}"
            )
        return _advance_gate_via_api(api, story_key, result)

    time.sleep(2.0)
    result.stage_gate_clicks.append(clicked)
    log.info("SPA advance clicked for %s: %s", story_key, clicked)
    return True


def _advance_gate_via_api(
    api: StoryApiClient, story_key: str, result: ScenarioResult
) -> bool:
    """Fallback: drive whichever gate is pending through the HTTP endpoints."""
    # Lifecycle gate (开发→测试) requires _story_state_gate; 409 if not pending.
    try:
        api.lifecycle_advance(story_key)
        result.stage_gate_clicks.append("lifecycle_advance (API)")
        log.info("lifecycle gate advanced via API for %s", story_key)
        return True
    except Exception:
        pass
    try:
        api.advance(story_key)
        result.stage_gate_clicks.append("stage advance (API)")
        log.info("stage gate advanced via API for %s", story_key)
        return True
    except Exception:
        return False
