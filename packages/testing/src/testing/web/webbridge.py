"""HTTP client for the Kimi WebBridge daemon (127.0.0.1:10086).

WebBridge is a local daemon + browser extension that lets any caller drive the
user's real Chrome — reusing their login sessions — via a small JSON-RPC over
HTTP. The skill in ``~/.claude/skills/kimi-webbridge/SKILL.md`` documents the
tool table; this module is the thin Python wrapper pytest calls directly so the
UI-driving layer is fully deterministic (no extra AI in the loop).

Wire contract (verified against the live daemon):
    POST /command  body ``{"action","args","session"}`` → ``{"ok":true,"data":{...}}``
    GET  /status                      → ``{"running","extension_connected","port",...}``

Snapshot shape: ``data.tree`` is a nested list of
``{role, name, ref?, children?}``. Interactive nodes carry ``ref`` like ``"@e3"``;
static text nodes usually don't. Match on ``name`` then use the node's ``ref``
with ``click``/``fill``.

Windows non-ASCII safety: PowerShell/cmd corrupt inline-JSON Chinese into ``?``
(SKILL.md:57-68). Every request body is written to a unique temp file and POSTed
via ``--data-binary @file`` — but since we're using httpx (not a shell), we pass
raw UTF-8 bytes directly, which sidesteps the shell entirely. The file-body path
is only needed if we ever shell out to curl.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

log = logging.getLogger("testing.web.webbridge")

DEFAULT_URL = "http://127.0.0.1:10086"
_DAEMON_BIN = Path.home() / ".kimi-webbridge" / "bin" / "kimi-webbridge.exe"


class WebBridgeError(RuntimeError):
    """WebBridge daemon unreachable or returned an error."""


class WebBridgeClient:
    """Drive the real Chrome browser through the WebBridge daemon.

    Each instance is bound to one ``session`` (= one tab group in the user's
    browser); pass the same session name for every call of a task so tabs group
    together (SKILL.md:70-87).
    """

    def __init__(
        self,
        *,
        session: str = "e2e",
        base_url: str = DEFAULT_URL,
        timeout: float = 30.0,
        ensure_daemon: bool = True,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        if ensure_daemon:
            self._ensure_daemon()

    # ---- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WebBridgeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        """Return the daemon status dict (``running``/``extension_connected``/...)."""
        r = self._client.get(f"{self.base_url}/status")
        r.raise_for_status()
        return r.json()

    def _ensure_daemon(self) -> None:
        """Start the daemon if it isn't up (SKILL.md:149-170). Idempotent.

        Raises :class:`WebBridgeError` if the daemon can't be reached or the
        browser extension isn't connected — tests then skip with a clear reason.
        """
        try:
            st = self.status()
        except httpx.ConnectError as exc:
            if not _try_start_daemon():
                raise WebBridgeError(
                    "WebBridge daemon unreachable at "
                    f"{self.base_url} and binary not found / failed to start: {exc}"
                ) from exc
            st = self.status()  # may raise if still down
        if not st.get("running"):
            raise WebBridgeError(f"WebBridge daemon not running: {st}")
        if not st.get("extension_connected"):
            raise WebBridgeError(
                "WebBridge daemon is up but the browser extension is not connected. "
                "Open Chrome/Edge so the WebBridge extension can attach, then retry."
            )

    # ---- low-level command dispatch --------------------------------------

    def command(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return the ``data`` payload.

        Raises :class:`WebBridgeError` if ``ok`` is false, the daemon returns a
        bad HTTP status (e.g. 502 when the browser extension errors), or the
        daemon is unreachable. Callers that treat the browser path as
        best-effort can catch ``WebBridgeError`` and fall back to the API.
        Auto-retries once after (re)starting the daemon on connection refusal.
        """
        body = {"action": action, "args": args or {}, "session": self.session}
        for attempt in (1, 2):
            try:
                r = self._client.post(f"{self.base_url}/command", json=body)
                r.raise_for_status()
                payload = r.json()
                if not payload.get("ok"):
                    raise WebBridgeError(f"{action} failed: {payload}")
                return payload.get("data") or {}
            except httpx.ConnectError:
                if attempt == 1 and _try_start_daemon():
                    continue
                raise WebBridgeError(
                    f"cannot reach WebBridge daemon at {self.base_url}"
                ) from None
            except httpx.HTTPStatusError as exc:
                # 502 etc. = daemon up but browser extension side failed. Not a
                # connection issue, so no daemon-restart retry; surface as
                # WebBridgeError so callers can fall back deterministically.
                raise WebBridgeError(
                    f"{action} daemon returned HTTP {exc.response.status_code}"
                ) from exc
        raise WebBridgeError(f"{action} exhausted retries")  # pragma: no cover

    # ---- tool wrappers (mirror SKILL.md:13-31) ---------------------------

    def navigate(
        self, url: str, *, new_tab: bool = True, group_title: str | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"url": url, "newTab": new_tab}
        if group_title:
            args["group_title"] = group_title
        return self.command("navigate", args)

    def find_tab(self, url: str, *, active: bool = False) -> dict[str, Any]:
        return self.command("find_tab", {"url": url, "active": active})

    def list_tabs(self) -> dict[str, Any]:
        return self.command("list_tabs", {})

    def close_session(self) -> dict[str, Any]:
        return self.command("close_session", {})

    def snapshot(self) -> dict[str, Any]:
        """Return ``{url, title, tree}``. ``tree`` is a nested node list."""
        return self.command("snapshot", {})

    def click(self, selector: str) -> dict[str, Any]:
        """``selector`` is an ``@eNN`` ref or a CSS selector."""
        return self.command("click", {"selector": selector})

    def fill(self, selector: str, value: str) -> dict[str, Any]:
        return self.command("fill", {"selector": selector, "value": value})

    def evaluate(self, code: str) -> dict[str, Any]:
        return self.command("evaluate", {"code": code})

    def click_dom_button(self, *, contains: str | None = None, exact: str | None = None) -> bool:
        """Find a <button> in the DOM by text and click it via JS (bypasses a11y).

        Fallback for buttons the accessibility-tree snapshot doesn't expose (the
        SPA's stage-gate "确认推进 → X" button renders inside a card that some
        WebBridge snapshot passes miss). Returns True if a button was found &
        clicked. Text match: exact first, else contains; visible buttons only.
        """
        import json as _json
        target = exact if exact is not None else (contains or "")
        use_exact = "1" if exact is not None else "0"
        code = (
            "(()=>{"
            "var bs=Array.from(document.querySelectorAll('button')).filter(b=>b.offsetParent!==null);"
            f"var t={_json.dumps(target)};"
            f"var exact={use_exact};"
            "var m = exact ? bs.find(b=>(b.textContent||'').trim()===t) "
            ": bs.find(b=>(b.textContent||'').indexOf(t)>=0);"
            "if(!m) return {ok:0};"
            "m.click();"
            "return {ok:1,text:(m.textContent||'').trim().slice(0,40)};"
            "})()"
        )
        res = self.evaluate(code)
        return bool((res.get("value") or {}).get("ok"))

    def screenshot(self, *, path: str | None = None, format: str = "png") -> dict[str, Any]:
        args: dict[str, Any] = {"format": format}
        if path:
            args["path"] = path
        return self.command("screenshot", args)

    # ---- text-matching helpers -------------------------------------------
    # The SPA's interactive buttons use dynamic labels (e.g.
    # ``"确认推进 → build"``, ``"进入 测试 →"``) so exact-text matching breaks.
    # snapshot → find the node by name prefix/exact → click its ``@e`` ref.

    @staticmethod
    def find_refs(
        tree: list[dict[str, Any]] | dict[str, Any],
        *,
        exact: str | None = None,
        prefix: str | None = None,
        contains: str | None = None,
        role: str | None = None,
    ) -> list[str]:
        """Walk the snapshot tree, return ``@e`` refs of matching nodes.

        A node matches if it has a ``ref`` AND its ``name`` satisfies the given
        text criterion (exact / prefix / contains), optionally filtered by role.
        """
        out: list[str] = []

        def matches(name: str) -> bool:
            if exact is not None and name == exact:
                return True
            if prefix is not None and name.startswith(prefix):
                return True
            if contains is not None and contains in name:
                return True
            return False

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for n in node:
                    walk(n)
                return
            if not isinstance(node, dict):
                return
            name = node.get("name") or ""
            ref = node.get("ref")
            if ref and (exact or prefix or contains):
                if matches(name) and (role is None or _role_eq(node.get("role"), role)):
                    out.append(ref)
            # When no text criterion given, still collect refs (optionally by role).
            if ref and not (exact or prefix or contains):
                if role is None or _role_eq(node.get("role"), role):
                    out.append(ref)
            for child in node.get("children") or []:
                walk(child)

        walk(tree)
        # de-dup, preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for r in out:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return uniq

    def click_text(
        self,
        *,
        exact: str | None = None,
        prefix: str | None = None,
        contains: str | None = None,
        role: str | None = None,
        tree: list[dict[str, Any]] | None = None,
        snapshot_age: float = 2.0,
    ) -> str:
        """Snapshot (if needed) + click the first matching node by ``@e`` ref.

        ``snapshot_age`` seconds is how stale an explicitly-passed ``tree`` may be
        before we re-snapshot. Returns the ref that was clicked.
        """
        snap = self.snapshot()
        nodes = snap.get("tree", [])
        refs = self.find_refs(nodes, exact=exact, prefix=prefix, contains=contains, role=role)
        if not refs:
            raise WebBridgeError(
                f"no element found: exact={exact!r} prefix={prefix!r} "
                f"contains={contains!r} role={role!r}"
            )
        self.click(refs[0])
        return refs[0]

    def fill_text(
        self,
        value: str,
        *,
        exact: str | None = None,
        prefix: str | None = None,
        contains: str | None = None,
        role: str | None = None,
    ) -> str:
        """Snapshot + fill the first matching node by ``@e`` ref."""
        snap = self.snapshot()
        nodes = snap.get("tree", [])
        refs = self.find_refs(nodes, exact=exact, prefix=prefix, contains=contains, role=role)
        if not refs:
            raise WebBridgeError(
                f"no element found to fill: exact={exact!r} prefix={prefix!r} "
                f"contains={contains!r} role={role!r}"
            )
        self.fill(refs[0], value)
        return refs[0]

    def page_text(self) -> str:
        """Flatten the current snapshot tree to a plain-text blob (for assertions)."""
        snap = self.snapshot()
        lines: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for n in node:
                    walk(n)
                return
            if isinstance(node, dict):
                name = (node.get("name") or "").strip()
                if name:
                    lines.append(name)
                for c in node.get("children") or []:
                    walk(c)

        walk(snap.get("tree", []))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Daemon bootstrap
# ---------------------------------------------------------------------------


def _try_start_daemon() -> bool:
    """Try to start the WebBridge daemon binary. Returns True if it (now) runs.

    Per SKILL.md:159-170 — ``start`` is idempotent; we never call stop/restart.
    """
    if not _DAEMON_BIN.exists():
        return False
    try:
        subprocess.run(
            [str(_DAEMON_BIN), "start"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("failed to start WebBridge daemon: %s", exc)
        return False
    # give it a moment to bind
    for _ in range(20):
        try:
            import httpx as _hx

            r = _hx.get(f"{DEFAULT_URL}/status", timeout=1.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def _role_eq(actual: Any, want: str) -> bool:
    """Case-insensitive role match (snapshot roles are like ``"button"``/``"Button"``)."""
    return str(actual or "").lower() == want.lower()
