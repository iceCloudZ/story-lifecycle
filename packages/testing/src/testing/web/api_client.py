"""httpx client mirroring story-lifecycle's FastAPI contract.

Used as a *deterministic backend probe* alongside the WebBridge-driven browser
path: seed a story over HTTP, stream the plan via SSE, poll status while the real
AI runs, then read back ``/diff`` / ``/llm-calls`` as judgement evidence. Pass /
fail is never decided here — this client only returns facts.

Contract source (all in ``orchestrator/service/api.py``):
    POST /api/story                       CreateStoryRequest  → camelCase row   (api.py:819)
    POST /api/story/{k}/start            StartStoryRequest?  → {ok,story_key}   (api.py:2940)
    GET  /api/story/{k}/plan                                  → {mode,stages,...}(api.py:3027)
    GET  /api/story/{k}/plan/stream       SSE  data:<json>   → type started/action/done (api.py:3130)
    POST /api/story/{k}/plan/confirm      {actions?}         → {ok,story_key}   (api.py:3199)
    PUT  /api/story/{k}/advance           {description?}     → stage-gate resume(api.py:855)
    POST /api/story/{k}/lifecycle/advance —                  → 开发→测试→上线   (api.py:884)
    GET  /api/story/{k}/clarify                              → {waiting,question?}(api.py:3338)
    POST /api/story/{k}/clarify/answer    {answer,id?}       → {ok,...}          (api.py:3366)
    GET  /api/story                       ?status=&show_all= → [summary]         (api.py:621)
    GET  /api/story/{k}                                      → detail(+ctxJson str)(api.py:688)
    GET  /api/story/{k}/llm-calls                             → {calls:[...]}    (api.py:777)
    GET  /api/story/{k}/diff                                  → {diff,files,...} (api.py:793)
    GET  /api/session/health                                 → {status:ok}      (api.py:1175)

Gotchas baked in:
    * ``contextJson``/``branchesJson`` in the detail payload are JSON **strings**;
      ``story()`` auto-parses them.
    * Error shapes are not uniform: query 404s give ``{"detail":...}``; ``/start``
      gives ``{"ok":false,"reasonCode":...}``. ``_check`` handles both.
    * Body-optional endpoints: FastAPI accepts ``{}`` when the model defaults.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator

import httpx

try:
    from httpx_sse import connect_sse
except ImportError:  # pragma: no cover - optional at import time, required for plan_stream
    connect_sse = None  # type: ignore[assignment]


class ApiError(RuntimeError):
    """Non-2xx backend response. Carries status + body for diagnostics."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class StoryApiClient:
    """Synchronous httpx client for the story-lifecycle backend."""

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ---- core ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        r = httpx.request(
            method, url, json=json_body, params=params, timeout=timeout or self._timeout
        )
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise ApiError(r.status_code, body)
        return body

    @staticmethod
    def _check(payload: Any) -> Any:
        """Raise if an ``{ok:false,...}`` envelope reports failure; else passthrough."""
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise ApiError(409, payload)
        return payload

    # ---- story create / start / plan ------------------------------------

    def create_story(
        self,
        key: str,
        *,
        title: str = "",
        workspace: str,
        profile: str = "minimal",
        content: str = "",
        autostart: bool = False,
    ) -> dict[str, Any]:
        """POST /api/story — lightweight create (api.py:819).

        ``autostart=False`` keeps the story parked; the e2e flow starts it
        explicitly via :meth:`start` so the plan-generation path runs through
        the real HTTP surface.
        """
        return self._check(
            self._request(
                "POST",
                "/api/story",
                json_body={
                    "key": key,
                    "title": title,
                    "workspace": workspace,
                    "profile": profile,
                    "content": content,
                    "autostart": autostart,
                },
            )
        )

    def start(self, key: str, *, content: str = "") -> dict[str, Any]:
        """POST /api/story/{key}/start — intake → PRD → planning (api.py:2940).

        Passing ``content`` short-circuits source-based PRD generation and avoids
        the 409 ``prd_generation_failed`` reason.
        """
        body = {"content": content} if content else {}
        return self._check(self._request("POST", f"/api/story/{key}/start", json_body=body))

    def plan(self, key: str) -> dict[str, Any]:
        """GET /api/story/{key}/plan (api.py:3027)."""
        return self._check(self._request("GET", f"/api/story/{key}/plan"))

    def plan_stream(self, key: str, *, timeout: float = 120.0) -> list[dict[str, Any]]:
        """GET /api/story/{key}/plan/stream — collect SSE events until ``done``/``error``.

        Each ``data:`` line is JSON with a ``type`` field: ``started`` |
        (action dicts) | ``done`` | ``error``. Returns the list of all events.
        """
        if connect_sse is None:  # pragma: no cover
            raise RuntimeError("httpx-sse not installed; cannot stream plan")
        events: list[dict[str, Any]] = []
        url = f"{self.base_url}/api/story/{key}/plan/stream"
        with httpx.Client(timeout=timeout) as client:
            with connect_sse(client, "GET", url) as event_source:
                if event_source.response.status_code >= 400:
                    raise ApiError(
                        event_source.response.status_code, "plan/stream failed"
                    )
                for sse in event_source.iter_sse():
                    try:
                        evt = json.loads(sse.data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    events.append(evt)
                    if evt.get("type") in {"done", "error"}:
                        break
        return events

    def confirm_plan(self, key: str, *, actions: list[dict] | None = None) -> dict[str, Any]:
        """POST /api/story/{key}/plan/confirm (api.py:3199). ``actions`` optional override."""
        body = {"actions": actions} if actions else {}
        return self._check(
            self._request("POST", f"/api/story/{key}/plan/confirm", json_body=body)
        )

    # ---- stage / lifecycle gates ----------------------------------------

    def advance(self, key: str, *, description: str = "") -> dict[str, Any]:
        """PUT /api/story/{key}/advance — resume from a stage gate (paused) (api.py:855)."""
        return self._check(
            self._request("PUT", f"/api/story/{key}/advance", json_body={"description": description})
        )

    def lifecycle_advance(self, key: str) -> dict[str, Any]:
        """POST /api/story/{key}/lifecycle/advance — 开发→测试→上线 (api.py:884).

        Requires ``_story_state_gate.awaiting_confirm``; 409 if no pending gate.
        """
        return self._check(
            self._request("POST", f"/api/story/{key}/lifecycle/advance", json_body=None)
        )

    # ---- HITL clarify ---------------------------------------------------

    def get_pending_clarify(self, key: str) -> dict[str, Any]:
        """GET /api/story/{key}/clarify (api.py:3338). ``{waiting, question?}``."""
        return self._check(self._request("GET", f"/api/story/{key}/clarify"))

    def answer_clarify(self, key: str, answer: str, *, qid: str | None = None) -> dict[str, Any]:
        """POST /api/story/{key}/clarify/answer (api.py:3366)."""
        body: dict[str, Any] = {"answer": answer}
        if qid:
            body["id"] = qid
        return self._check(
            self._request("POST", f"/api/story/{key}/clarify/answer", json_body=body)
        )

    # ---- queries --------------------------------------------------------

    def list_stories(self, **params: Any) -> list[dict[str, Any]]:
        """GET /api/story — optional filters: status/show_all/show_completed/..."""
        return self._check(self._request("GET", "/api/story", params=params or None))

    def story(self, key: str) -> dict[str, Any]:
        """GET /api/story/{key} — auto-parse the stringified JSON fields."""
        data = self._check(self._request("GET", f"/api/story/{key}"))
        for field in ("contextJson", "branchesJson"):
            raw = data.get(field)
            if isinstance(raw, str) and raw:
                try:
                    data[field] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        return data

    def llm_calls(self, key: str) -> list[dict[str, Any]]:
        """GET /api/story/{key}/llm-calls → the audit rows (api.py:777)."""
        payload = self._check(self._request("GET", f"/api/story/{key}/llm-calls"))
        return payload.get("calls", []) if isinstance(payload, dict) else payload

    def diff(self, key: str) -> dict[str, Any]:
        """GET /api/story/{key}/diff (api.py:793)."""
        return self._check(self._request("GET", f"/api/story/{key}/diff"))

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/session/health")

    # ---- polling helper -------------------------------------------------

    def wait_until(
        self,
        predicate: Callable[["StoryApiClient"], bool],
        *,
        timeout: float = 600.0,
        interval: float = 2.0,
        desc: str = "condition",
    ) -> None:
        """Block until ``predicate(self)`` is truthy or ``timeout`` elapses.

        Story execution is asynchronous (real AI in a thread pool), so the test
        layer polls backend state rather than assuming synchronous completion.
        """
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if predicate(self):
                    return
            except Exception as exc:  # transient 404 while story is mid-write
                last_err = exc
            time.sleep(interval)
        raise TimeoutError(
            f"wait_until timed out after {timeout}s waiting for {desc}"
            + (f": last error {last_err!r}" if last_err else "")
        )

    def story_status(self, key: str) -> str:
        """Convenience: current status string of a story."""
        return str(self.story(key).get("status") or "")
