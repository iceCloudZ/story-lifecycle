"""Real uvicorn server fixture for WebBridge E2E.

Why a *real* uvicorn process (not FastAPI TestClient):
    WebBridge drives an external Chrome browser — an OS process outside pytest.
    That browser can only reach the backend over a real TCP socket
    (``127.0.0.1:<port>``). ``fastapi.testclient.TestClient`` is in-process ASGI
    and invisible to any external process, so it cannot serve the browser, the
    real WebSocket handshake (``/ws/stories``), or the real SSE chunked stream
    (``/plan/stream``). We must start an actual uvicorn server.

Approach — same-process background thread + dedicated asyncio loop:
    * Pass the **app object** to ``uvicorn.Config(app=app, ...)`` (not a string
      import path) so uvicorn reuses the already-imported, already-monkeypatched
      app instance — no re-import, no second config gate.
    * Run ``server.serve()`` on a private asyncio loop in a daemon thread; tear
      down via ``server.should_exit = True`` (no signal handlers, which are
      main-thread-only and unsafe in a worker thread).
    * Reuse the autouse ``_isolated_db`` fixture: it already ``setenv STORY_HOME``
      (``packages/story-lifecycle/tests/conftest.py:66``) and ``get_db_path()``
      reads that env **live** on every call
      (``infra/db/models.py:66-71``), so the uvicorn worker thread — which lives
      in the same process — resolves the same temp DB with zero extra work.

Known quirk fixed here: ``orchestrator/engine/graph.py:20,32-33`` freezes
``STORY_HOME`` / ``_workspace_locks_dir`` as module-level constants at import
time (they point at the real ``~/.story-lifecycle``). The existing conftest only
patches ``nodes_mod.STORY_HOME``, not ``graph``'s copy. This fixture patches both
so workspace locks land under the temp home too.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import httpx

log = logging.getLogger("testing.web.server")

# Health endpoint polled to know the server is ready (api.py:1175-1177).
_HEALTH_PATH = "/api/session/health"
_DEFAULT_READY_TIMEOUT = 20.0


class RunningServer:
    """Handle to a live uvicorn server + its public URLs.

    Callers use ``base_url`` / ``ws_base_url``; the uvicorn ``server`` handle is
    kept private so the fixture can stop it on teardown.
    """

    def __init__(self, base_url: str, ws_base_url: str, port: int, server, thread):
        self.base_url = base_url
        self.ws_base_url = ws_base_url
        self.port = port
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        """Signal the server to exit and join its worker thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15.0)


def _pick_port() -> int:
    """Grab an ephemeral free port on 127.0.0.1.

    Small TOCTOU window between close() and uvicorn binding, but acceptable for
    sequential runs; pytest-xdist is not the default here.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _redirect_graph_home(tmp_home: str) -> None:
    """Point graph.py's import-time-frozen paths at the temp home.

    ``graph.STORY_HOME`` (graph.py:20) and ``_workspace_locks_dir`` (graph.py:32)
    are module-level constants evaluated at import; they already mkdir'd the real
    ``~/.story-lifecycle/workspace-locks`` once. Redirecting now keeps *new* lock
    files off the real home for the rest of the test process.
    """
    try:
        from pathlib import Path

        from story_lifecycle.orchestrator.engine import graph

        home = Path(tmp_home)
        graph.STORY_HOME = home
        locks_dir = home / "workspace-locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        graph._workspace_locks_dir = locks_dir
    except Exception as exc:  # pragma: no cover - best-effort, non-fatal
        log.warning("could not redirect graph.STORY_HOME: %s", exc)


def _load_llm_env() -> None:
    """Populate ``STORY_LLM_*`` env from config.yaml so the server's LLMClient works.

    The CLI path (``_run_server``) calls ``load_config_to_env()`` before booting
    uvicorn; we bypass the CLI to skip its interactive setup gate, so we call the
    same loader ourselves. It is idempotent (only sets env when unset), so any
    CI-provided env vars take precedence.
    """
    try:
        from story_lifecycle.entry.cli.setup import load_config_to_env

        load_config_to_env()
    except Exception as exc:  # pragma: no cover - best-effort
        log.warning("load_config_to_env failed: %s", exc)


def start_uvicorn_server(
    tmp_home: str,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    ready_timeout: float = _DEFAULT_READY_TIMEOUT,
) -> RunningServer:
    """Boot a real uvicorn server in a background thread against ``tmp_home``.

    Must be called from the main test thread (where ``_isolated_db`` set
    ``STORY_HOME``). The server shares the process and therefore the env-based
    DB isolation.
    """
    import uvicorn

    from story_lifecycle.orchestrator.service.api import app

    _redirect_graph_home(tmp_home)
    _load_llm_env()

    port = port or _pick_port()
    # lifespan="on" so api.py:lifespan runs (init_db / recover / done-file watcher).
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        lifespan="on",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # Block uvicorn from installing signal handlers — they only work on the main
    # thread and raise if touched from our worker thread.
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(
        target=_run_server_loop, args=(server,), name="webbridge-uvicorn", daemon=True
    )
    thread.start()

    base_url = f"http://{host}:{port}"
    _wait_ready(base_url, ready_timeout)
    return RunningServer(
        base_url=base_url,
        ws_base_url=f"ws://{host}:{port}",
        port=port,
        server=server,
        thread=thread,
    )


def _run_server_loop(server) -> None:
    """Run ``server.serve()`` on a fresh asyncio loop owned by this thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    except BaseException:  # pragma: no cover - surface boot failure in logs
        log.exception("uvicorn worker thread crashed")
        raise


def _wait_ready(base_url: str, timeout: float) -> None:
    """Poll the health endpoint until the server answers 200."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    with httpx.Client(base_url=base_url, timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(_HEALTH_PATH).status_code == 200:
                    return
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                last_err = exc
            time.sleep(0.2)
    raise RuntimeError(
        f"uvicorn server not ready at {base_url}{_HEALTH_PATH} within {timeout}s"
        + (f": {last_err}" if last_err else "")
    )
