"""桌面令牌代理(DESIGN-story-butler §3.3,WP3)—— 微信回方向链路的桌面收口。

**部署形态(隧道→代理→serve 三段)**::

    clawbot(101,微信 iLink 双向 bot,纯管道无 LLM)
      │  http://127.0.0.1:18180(101 本机视角:反向隧道口,sshd GatewayPorts=no 只绑 loopback)
      ▼
    反向 SSH 隧道(桌面出站 ``ssh -N -R 18180:127.0.0.1:18181``,零入站端口,WP5 保活)
      ▼
    本代理 127.0.0.1:18181 —— X-Internal-Token 执法 + 白名单路由 + 请求日志
      ▼
    story serve http://127.0.0.1:8180(本地信任模型,不加全局鉴权 —— §3.3:
    代理只挂在隧道口,收口面最小;给 serve 全局加鉴权是 §8 anti-pattern)

**env 清单(全部调用时读,测试可注入)**:

- ``BUTLER_PROXY_PORT``   代理监听端口,默认 ``18181``。
- ``STORY_SERVE_URL``     转发目标,默认 ``http://127.0.0.1:8180``。
- ``BUTLER_PROXY_TOKEN``  共享令牌。**必须配置**:未配置(空/缺省)时安全默认生效,
  一切请求 401(代理照常起,隧道心跳能看到「代理活着但全拒」的状态)。
- ``STORY_LOG_DIR``       请求日志目录(可选);缺省 ``<repo>/ws/``(从 cwd 向上找
  .git 定位 repo 根;ws/ 已 gitignore)。日志文件名固定 ``butler-proxy.log``。

**token 怎么配**:桌面与 101 clawbot 两侧放同一个高熵随机串(桌面
``set BUTLER_PROXY_TOKEN=<串>`` 后启动本代理;101 侧按 WP4 的 internal-API+token
惯例配进 clawbot),每个请求带 ``X-Internal-Token`` 头,恒定时间比对,不等即 401。

**白名单路由(白名单外一律 404,含 method 不符)**:

========================================  ==========================================
代理路径                                   serve 路径(实际转发)
========================================  ==========================================
``GET /status``                           (不发转发)实时探测 ``GET /api/story``(3s
                                          超时)→ ``{ok, proxy:"alive", serve:"up"|"down"}``;
                                          serve down 返回 200 —— 这是隧道心跳,不是代理故障
``GET /story/{key}/brief``                ``GET /api/story/{key}``(透传)
``GET /patrol/summary``                   ``GET /api/story``(列表徽标聚合:6f371215
                                          起 story 列表自带每 story 的 ``patrolSummary``,
                                          WP2 patrol_summary 工具同源)
``POST /story/{key}/advance``             先查 ``GET /api/story/{key}`` 的 ``confirmGates``:
                                          body 里的 target 命中任何 ``targetIsTerminal=true``
                                          的门 → 403 拒绝(**微信通道不做终态确认,§8**);
                                          否则 ``POST /api/story/{key}/lifecycle/advance``,
                                          body 带 ``confirmed_via:"wechat"``(账本,§4),
                                          serve 应答原样透传(含 409/428)
========================================  ==========================================

**请求日志**:每请求一行 append(时间 | 方法 | 路径 | token 校验结果 | 上游状态码
| 耗时 ms);代理本地拒绝(401/403/404/400)上游状态码记 ``-``,透传/探测记 serve
的真实状态码。

**实现约束**:仅标准库(http.server ThreadingHTTPServer + urllib.request),不引
新依赖 —— 本件属 §2 拓扑的「桥层」,可抛弃件,依赖越薄越可重写。

**启动**::

    python -m story_lifecycle.butler_proxy     # 顶层 shim → 本模块 main()
    story butler-proxy                         # CLI 最薄入口(entry/cli/main.py)

bind 失败(端口占用)给清晰中文报错并以退出码 1 结束;Ctrl+C 优雅关停。
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- 常量(§2 拓扑的桌面侧默认值;env 可覆盖) ----

DEFAULT_PROXY_PORT = 18181
DEFAULT_SERVE_URL = "http://127.0.0.1:8180"

TOKEN_HEADER = "X-Internal-Token"

# /status 的 serve 探测超时(§3.3:心跳用,必须快;其余转发用 FORWARD_TIMEOUT)
STATUS_PROBE_TIMEOUT = 3.0
FORWARD_TIMEOUT = 10.0

LOG_FILE_NAME = "butler-proxy.log"

# advance 合法续推的账本来源(§3.1:微信回 T 确认,代理侧固定打上 wechat)
ADVANCE_CONFIRMED_VIA = "wechat"

# 代理本地拒绝(无上游)时日志 upstream 列的占位
UPSTREAM_LOCAL = "-"


class ButlerProxyBindError(RuntimeError):
    """代理监听端口失败(典型:端口被占用)。中文 message 直接可展示。"""


class ServeUnreachable(RuntimeError):
    """转发目标(serve)连接失败/超时 —— /status 记 down,其余路由 502。"""


# ---- 配置读取(函数化而非 import 时读死,测试改 env 即生效) ----


def proxy_port() -> int:
    """代理监听端口(env ``BUTLER_PROXY_PORT`` > 默认 18181)。"""
    raw = os.environ.get("BUTLER_PROXY_PORT") or ""
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PROXY_PORT


def serve_base_url() -> str:
    """转发目标(env ``STORY_SERVE_URL`` > 默认 ``http://127.0.0.1:8180``)。"""
    return (os.environ.get("STORY_SERVE_URL") or DEFAULT_SERVE_URL).rstrip("/")


def proxy_token() -> str:
    """共享令牌(env ``BUTLER_PROXY_TOKEN``);空串 = 未配置 → 一切请求 401。"""
    return (os.environ.get("BUTLER_PROXY_TOKEN") or "").strip()


def _find_repo_root(start: Path) -> Path | None:
    """从 start 向上找 .git 定位仓库根(找不到返回 None)。"""
    for p in (start, *start.parents):
        if (p / ".git").exists():
            return p
    return None


def log_file_path() -> Path:
    """请求日志文件路径。

    优先 ``STORY_LOG_DIR``;否则从 cwd 向上找 .git 取 ``<repo>/ws/``(gitignore
    已覆盖);再退而求其次 ``<cwd>/ws/``。文件名固定 ``butler-proxy.log``。
    """
    env_dir = os.environ.get("STORY_LOG_DIR") or ""
    if env_dir:
        return Path(env_dir) / LOG_FILE_NAME
    root = _find_repo_root(Path.cwd())
    base = (root or Path.cwd()) / "ws"
    return base / LOG_FILE_NAME


# ---- 请求日志(append 一行;线程锁串行化,ThreadingHTTPServer 多线程写) ----

_LOG_LOCK = threading.Lock()


def append_request_log(
    log_path: Path,
    method: str,
    path: str,
    token_state: str,
    upstream_status: object,
    elapsed_ms: int,
) -> None:
    """追加一行请求日志:时间 | 方法 | 路径 | token 校验结果 | 上游状态码 | 耗时 ms。

    代理本地拒绝(token/白名单/终态)``upstream_status`` 传 ``-``;透传与 /status
    探测传 serve 的真实状态码。落盘失败静默(转发职责不因日志故障停摆)。
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = (
        f"{ts} | {method} | {path} | token={token_state} "
        f"| upstream={upstream_status} | {elapsed_ms}ms\n"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


# ---- 白名单路由(纯函数,便于单测) ----


def match_route(method: str, path: str) -> tuple[str, str] | None:
    """白名单匹配。命中返回 ``(路由名, story_key)``,不命中(含 method 不符)None。

    白名单 = ``GET /status``、``GET /patrol/summary``、``GET /story/{key}/brief``、
    ``POST /story/{key}/advance``。key 做 URL 解码,空段不匹配。
    """
    parts = [p for p in path.split("/") if p != ""]
    method = method.upper()
    if method == "GET" and path.rstrip("/") == "/status" and len(parts) == 1:
        return ("status", "")
    if method == "GET" and path.rstrip("/") == "/patrol/summary" and len(parts) == 2:
        return ("patrol_summary", "")
    if len(parts) == 3 and parts[0] == "story" and parts[2] in ("brief", "advance"):
        if parts[2] == "brief" and method != "GET":
            return None
        if parts[2] == "advance" and method != "POST":
            return None
        key = urllib.parse.unquote(parts[1]).strip()
        if not key:
            return None
        return (parts[2], key)
    return None


# ---- 上游转发(唯一 I/O 面;urllib 标准库,serve 应答 4xx/5xx 也算到达) ----

# 回方向转发只打 loopback,**绝不绕道系统代理**:http_proxy/clash 一类全局代理
# 会把 127.0.0.1 的请求也截走(慢、超时甚至假 502)。ProxyHandler({}) = 显式空
# 代理表,forward 永远直连。双重检查锁,进程内只建一次。
_OPENER_LOCK = threading.Lock()
_NO_PROXY_OPENER: urllib.request.OpenerDirector | None = None


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    global _NO_PROXY_OPENER
    if _NO_PROXY_OPENER is None:
        with _OPENER_LOCK:
            if _NO_PROXY_OPENER is None:
                _NO_PROXY_OPENER = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                )
    return _NO_PROXY_OPENER


def forward(
    base_url: str,
    method: str,
    path: str,
    body: bytes | None = None,
    timeout: float = FORWARD_TIMEOUT,
) -> tuple[int, bytes, str]:
    """一次到 serve 的 HTTP 调用,返回 ``(status, body bytes, content-type)``。

    serve 的 4xx/5xx 原样返回(调用方透传);连接层失败(不在/超时)抛
    :class:`ServeUnreachable`。
    """
    url = f"{base_url}{path}"
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _no_proxy_opener().open(req, timeout=timeout) as resp:
            return (
                int(resp.status),
                resp.read(),
                resp.headers.get("Content-Type") or "application/json",
            )
    except urllib.error.HTTPError as e:  # 4xx/5xx:serve 在,应答原样透传
        ctype = e.headers.get("Content-Type") if e.headers else None
        return int(e.code), e.read() or b"", ctype or "application/json"
    except Exception as e:  # URLError/ConnectionError/timeout → serve 不可达
        raise ServeUnreachable(f"{method} {path} → {base_url}: {e}") from e


# ---- advance 终态执法(§8 anti-pattern「微信通道出现终态确认」的服务端闸) ----


def _extract_target(req_obj: dict) -> str:
    """从 advance 请求 body 里取目标态(clawbot 侧约定 ``target``;别名宽容)。"""
    for k in ("target", "targetState", "target_state"):
        v = req_obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def terminal_gate_hit(gates: object, target: str) -> dict | None:
    """target 是否命中终态门(纯函数)。命中返回那个门,否则 None。

    规则(§3.3):请求 target 精确等于某个 ``targetIsTerminal=true`` 门的
    ``targetState`` → 命中。target 缺省时 **fail-closed**:只要挂着终态门就算命中
    (微信通道不允许终态门悬着时盲推;终态集合由 serve 侧判定返回,本代理不
    本地硬编码 上线/结项 —— 与 WP2 butler_server 同一原则)。
    """
    for g in gates if isinstance(gates, list) else []:
        if not isinstance(g, dict) or not g.get("targetIsTerminal"):
            continue
        gate_target = str(g.get("targetState") or "")
        if not target or gate_target == target:
            return g
    return None


def _terminal_refusal(gate: dict, target: str) -> dict:
    """403 拒绝体(中文说明,指向桌面评审 skill / Story 详情页 UI)。"""
    shown = str(gate.get("targetState") or target or "(未指定)")
    return {
        "ok": False,
        "refused": "terminal_target",
        "summary": (
            f"目标态「{shown}」是终态/发布类跃迁,微信通道不做终态确认"
            f"(DESIGN-story-butler §8):请在桌面评审 skill / Story 详情页 UI "
            f"以完整上下文完成该确认。"
        ),
        "gate": gate,
    }


# ---- HTTP handler ----

# 路由 handler 的返回形态:(最终状态码, body, content-type, 日志 upstream 列)
RouteResult = tuple[int, bytes, str, object]


class ButlerProxyHandler(BaseHTTPRequestHandler):
    """白名单代理 handler。配置(serve 地址/token/日志路径)挂在 server 实例上。"""

    protocol_version = "HTTP/1.1"
    server_version = "ButlerProxy/1.0"

    # ---- 基建 ----

    def log_message(self, fmt, *args):  # noqa: A002 — http.server 固定签名
        """静默默认 stderr 访问日志 —— 请求审计统一走 butler-proxy.log。"""

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_json(self, status: int, obj: dict) -> RouteResult:
        """代理本地生成的 JSON 应答(401/403/404/400/502),upstream 列记 ``-``。"""
        return (
            status,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            UPSTREAM_LOCAL,
        )

    @property
    def _unreachable_json(self) -> RouteResult:
        return self._local_json(
            502,
            {
                "ok": False,
                "refused": "serve_unreachable",
                "summary": (
                    f"管家后端不可达({self.server.serve_url}),请先 `story serve`。"
                ),
            },
        )

    # ---- 入口 ----

    def do_GET(self) -> None:  # noqa: N802 — http.server 固定命名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        """统一入口:token 执法 → 白名单 → 路由 handler → 落一行日志 → 应答。

        日志在**应答之前**落盘:客户端看到响应时这行请求日志已在盘上(审计
        不丢,也让测试无需等待线程收尾)。
        """
        started = time.monotonic()
        path = urllib.parse.urlsplit(self.path).path
        token_state, authed = self._check_token()
        try:
            if not authed:
                result = self._local_json(
                    401,
                    {
                        "ok": False,
                        "refused": "unauthorized",
                        "summary": _token_refusal_text(token_state),
                    },
                )
            else:
                route = match_route(method, path)
                if route is None:
                    result = self._local_json(
                        404,
                        {
                            "ok": False,
                            "refused": "not_whitelisted",
                            "summary": (
                                f"路径不在白名单:{method} {path}。可用:"
                                "GET /status、GET /story/{key}/brief、"
                                "GET /patrol/summary、POST /story/{key}/advance。"
                            ),
                        },
                    )
                else:
                    handler = {
                        "status": self._route_status,
                        "patrol_summary": self._route_patrol_summary,
                        "brief": self._route_brief,
                        "advance": self._route_advance,
                    }[route[0]]
                    result = handler(route[1])
        except Exception:  # noqa: BLE001 — handler 兜底,不让工作线程带异常死掉
            result = self._local_json(
                502,
                {
                    "ok": False,
                    "refused": "proxy_error",
                    "summary": "代理内部转发失败,请查 butler-proxy.log 与 serve 状态。",
                },
            )
        status, body, ctype, upstream = result
        elapsed = int((time.monotonic() - started) * 1000)
        append_request_log(
            self.server.log_path,  # type: ignore[attr-defined]
            method,
            path,
            token_state,
            upstream,
            elapsed,
        )
        try:
            self._send_bytes(status, body, ctype)
        except OSError:  # 客户端先断开等 —— 日志已落,不必让线程带异常死掉
            pass

    # ---- token 执法 ----

    def _check_token(self) -> tuple[str, bool]:
        """校验 X-Internal-Token。返回 (状态, 是否放行)。

        env 未配置(安全默认)→ 一律 401;配置了 → 恒定时间比对(hmac.compare_digest)。
        """
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected:
            return ("unconfigured", False)
        got = self.headers.get(TOKEN_HEADER) or ""
        if got and hmac.compare_digest(got, expected):
            return ("ok", True)
        return ("mismatch" if got else "missing", False)

    # ---- 四条白名单路由(返回 RouteResult) ----

    def _route_status(self, _key: str) -> RouteResult:
        """GET /status → 隧道心跳:探测 serve(3s),down 也是 200(代理没病)。"""
        try:
            probe_status, _, _ = forward(
                self.server.serve_url,  # type: ignore[attr-defined]
                "GET",
                "/api/story",
                timeout=STATUS_PROBE_TIMEOUT,
            )
        except ServeUnreachable:
            payload = {"ok": True, "proxy": "alive", "serve": "down"}
            return (
                200,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                UPSTREAM_LOCAL,
            )
        payload = {"ok": True, "proxy": "alive", "serve": "up"}
        return (
            200,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            probe_status,
        )

    def _route_patrol_summary(self, _key: str) -> RouteResult:
        """GET /patrol/summary → GET /api/story(列表徽标聚合,patrolSummary 同源)。"""
        return self._passthrough("GET", "/api/story", None)

    def _route_brief(self, key: str) -> RouteResult:
        """GET /story/{key}/brief → GET /api/story/{key}(透传)。"""
        return self._passthrough("GET", f"/api/story/{urllib.parse.quote(key)}", None)

    def _route_advance(self, key: str) -> RouteResult:
        """POST /story/{key}/advance → 终态执法后 POST /lifecycle/advance(wechat)。

        三步(§3.3):①查门 GET /api/story/{key}(serve 不在→502;查无 story 等
        4xx→原样透传);②target 命中终态门→403 拒绝不转发;③合法续推 POST
        /api/story/{key}/lifecycle/advance,body 带 confirmed_via=wechat,serve
        应答(含 409/428)原样透传。
        """
        raw = self._read_body()
        try:
            req_obj = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return self._local_json(
                400,
                {
                    "ok": False,
                    "refused": "bad_request",
                    "summary": '请求 body 不是合法 JSON(应为 {"target": "<目标态>"})。',
                },
            )
        if not isinstance(req_obj, dict):
            return self._local_json(
                400,
                {
                    "ok": False,
                    "refused": "bad_request",
                    "summary": '请求 body 应为 JSON 对象,如 {"target": "测试"}。',
                },
            )
        target = _extract_target(req_obj)

        # ① 查门
        try:
            gate_status, body, ctype = forward(
                self.server.serve_url,  # type: ignore[attr-defined]
                "GET",
                f"/api/story/{urllib.parse.quote(key)}",
                timeout=FORWARD_TIMEOUT,
            )
        except ServeUnreachable:
            return self._unreachable_json
        if gate_status >= 400:  # 查无 story(404)等 —— 让 serve 自己说话
            return (gate_status, body, ctype, gate_status)

        try:
            story = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            story = {}
        gates = story.get("confirmGates") if isinstance(story, dict) else None

        # ② 终态执法:命中 → 403 拒绝,绝不转发(微信通道不做终态确认,§8)
        hit = terminal_gate_hit(gates, target)
        if hit is not None:
            return self._local_json(403, _terminal_refusal(hit, target))

        # ③ 合法续推:confirmed_via=wechat 落账本(routers/lifecycle 透传字段)
        fwd_body = dict(req_obj)
        fwd_body["confirmed_via"] = ADVANCE_CONFIRMED_VIA
        return self._passthrough(
            "POST",
            f"/api/story/{urllib.parse.quote(key)}/lifecycle/advance",
            json.dumps(fwd_body, ensure_ascii=False).encode("utf-8"),
        )

    # ---- 小工具 ----

    def _passthrough(
        self, method: str, path: str, body: bytes | None
    ) -> RouteResult:
        """转发并把 serve 应答原样透传;连接失败 → 502 友好中文。"""
        try:
            status, resp_body, ctype = forward(
                self.server.serve_url,  # type: ignore[attr-defined]
                method,
                path,
                body=body,
                timeout=FORWARD_TIMEOUT,
            )
        except ServeUnreachable:
            return self._unreachable_json
        return (status, resp_body, ctype, status)


def _token_refusal_text(token_state: str) -> str:
    if token_state == "unconfigured":
        return (
            "代理未配置 BUTLER_PROXY_TOKEN(安全默认拒绝一切请求):"
            "请设置环境变量后重启代理。"
        )
    if token_state == "missing":
        return f"缺少 {TOKEN_HEADER} 请求头,已拒绝。"
    return f"{TOKEN_HEADER} 不匹配,已拒绝。"


# ---- server 装配 ----


class ButlerProxyServer(ThreadingHTTPServer):
    """代理 server:配置挂实例,handler 机械读取(不按路由分支散落配置)。"""

    daemon_threads = True
    # 关闭 SO_REUSEADDR:Windows 上它允许双绑同端口(占用探测失真),关掉才能让
    # 「端口被占用」在 bind 时干净地报 OSError → 中文报错(§3.3 交付要求)。
    allow_reuse_address = False

    def __init__(
        self,
        addr: tuple[str, int],
        serve_url: str,
        token: str,
        log_path: Path,
    ):
        self.serve_url = serve_url.rstrip("/")
        self.token = token
        self.log_path = Path(log_path)
        super().__init__(addr, ButlerProxyHandler)


def make_server(
    host: str = "127.0.0.1",
    port: int | None = None,
    serve_url: str | None = None,
    token: str | None = None,
    log_path: str | Path | None = None,
) -> ButlerProxyServer:
    """装配代理 server(显式参数优先,否则读 env;port=0 供测试取临时端口)。"""
    port = proxy_port() if port is None else port
    serve_url = serve_base_url() if serve_url is None else serve_url
    token = proxy_token() if token is None else token
    log_path = log_file_path() if log_path is None else Path(log_path)
    try:
        return ButlerProxyServer((host, port), serve_url, token, log_path)
    except OSError as e:
        raise ButlerProxyBindError(
            f"令牌代理无法监听 {host}:{port} —— 端口可能被占用({e})。"
            f"可用环境变量 BUTLER_PROXY_PORT 换端口,或停掉占用该端口的进程后重试。"
        ) from e


def main(port: int | None = None) -> int:
    """``python -m story_lifecycle.butler_proxy`` / ``story butler-proxy`` 入口。

    ``port`` 显式传入(CLI ``--port``)优先于 env ``BUTLER_PROXY_PORT``。
    """
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    token = proxy_token()
    try:
        server = make_server(port=port)
    except ButlerProxyBindError as e:
        print(str(e), file=sys.stderr)
        return 1

    actual_port = server.server_address[1]
    print(f"[butler-proxy] 监听 http://127.0.0.1:{actual_port}(桌面令牌代理,§3.3)")
    print(f"[butler-proxy] 转发目标 {server.serve_url}")
    if not token:
        print(
            "[butler-proxy] 警告:未配置 BUTLER_PROXY_TOKEN,所有请求将被拒绝(401,"
            "安全默认)。配置后重启代理才可服务微信回方向链路。"
        )
    print(f"[butler-proxy] 请求日志 {server.log_path}")
    print("[butler-proxy] Ctrl+C 优雅关停。")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[butler-proxy] 收到 Ctrl+C,正在优雅关停……")
    finally:
        server.server_close()
    print("[butler-proxy] 已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
