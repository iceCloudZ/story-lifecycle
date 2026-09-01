"""管家 WP3 桌面令牌代理测试(DESIGN-story-butler §3.3/§9 WP3)。

**不真依赖 :8180**:起一个 stub ThreadingHTTPServer 当假 serve(记录收到的
method/path/body,按 (method, path) 返回罐头响应),代理经 ``make_server(port=0,
serve_url=stub, token=..., log_path=tmp)`` 装配在临时端口,纯进程内闭环。

覆盖:
- token 执法:无头/错头/未配置 → 401(安全默认);
- 白名单:白名单外路径与 method 不符 → 404(match_route 纯函数另有单测);
- /status:serve up/down 两态(down 也 200 —— 隧道心跳语义);
- brief/patrol 转发透传(断言转发目标路径与响应体);
- advance:非终态门 → 转发且 body 含 confirmed_via=wechat;终态门 → 403 拒绝
  绝不转发;无 confirmGates → 透传 serve 原响应(含 409);
- 请求日志落盘格式(时间|方法|路径|token|上游状态码|耗时 ms)。
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from story_lifecycle.infra.butler_proxy import (
    ButlerProxyBindError,
    LOG_FILE_NAME,
    append_request_log,
    log_file_path,
    make_server,
    match_route,
    proxy_port,
    proxy_token,
    serve_base_url,
)

TOKEN = "butler-test-token"
KEY = "tapd-butler-wp3"


# ---- 假 serve(stub,记录请求 + 罐头响应) ----


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002 — 静默
        return

    def _handle(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        self.server.requests.append(
            {"method": method, "path": self.path, "body": body}
        )
        resp = self.server.responses.get((method, self.path))
        if resp is None:
            payload, status = {"detail": f"stub 无此路由: {method} {self.path}"}, 404
        else:
            status, payload = resp
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")


class _StubServe:
    """假 serve:responses 为 {(method, path): (status, json_obj)};requests 记录。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests: list[dict] = []
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._http.responses = self.responses
        self._http.requests = self.requests
        self.port = self._http.server_address[1]
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join(timeout=5)


class _Proxy:
    """被测代理:make_server 临时端口装配,后台线程 serve_forever。"""

    def __init__(self, serve_url, token=TOKEN, log_path=None):
        self.http = make_server(
            port=0, serve_url=serve_url, token=token, log_path=log_path
        )
        self.port = self.http.server_address[1]
        self._thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self._thread.join(timeout=5)


# ---- fixtures / 客户端 ----


@pytest.fixture()
def log_file(tmp_path):
    return tmp_path / LOG_FILE_NAME


@pytest.fixture()
def stub():
    s = _StubServe()
    yield s
    s.stop()


@pytest.fixture()
def proxy(stub, log_file):
    p = _Proxy(stub.base_url, token=TOKEN, log_path=log_file)
    yield p
    p.stop()


def _request(method, url, token=TOKEN, body=None):
    """经代理打一发,返回 (status, 解析后的 JSON)。4xx/5xx 也正常返回。

    客户端同样用显式空代理表(测试环境可能注入 http_proxy,loopback 不绕道)。"""
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["X-Internal-Token"] = token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            return int(resp.status), json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return int(e.code), json.loads(e.read() or b"{}")


def _proxy_url(proxy, path):
    return f"http://127.0.0.1:{proxy.port}{path}"


def _read_log(log_file) -> list[str]:
    return log_file.read_text(encoding="utf-8").splitlines()


def _story_detail(gates):
    return {
        "storyKey": KEY,
        "title": "管家 WP3 代理测试",
        "status": "paused",
        "lifecycleState": "开发",
        "currentStage": "build",
        "confirmGates": gates,
    }


# ---- token 执法 ----


class TestTokenEnforcement:
    def test_missing_token_401(self, proxy, log_file):
        status, body = _request("GET", _proxy_url(proxy, "/status"), token=None)
        assert status == 401
        assert body["refused"] == "unauthorized"
        assert "X-Internal-Token" in body["summary"]
        assert any("token=missing" in line for line in _read_log(log_file))

    def test_wrong_token_401(self, proxy, log_file):
        status, body = _request("GET", _proxy_url(proxy, "/status"), token="wrong")
        assert status == 401
        assert body["refused"] == "unauthorized"
        assert any("token=mismatch" in line for line in _read_log(log_file))

    def test_unconfigured_token_401_secure_default(self, stub, log_file):
        """token 未配置(安全默认):代理照常起,但一切请求 401(含 /status)。"""
        p = _Proxy(stub.base_url, token="", log_path=log_file)
        try:
            status, body = _request(
                "GET", f"http://127.0.0.1:{p.port}/status", token=TOKEN
            )
            assert status == 401
            assert "BUTLER_PROXY_TOKEN" in body["summary"]
            assert any(
                "token=unconfigured" in line for line in _read_log(log_file)
            )
        finally:
            p.stop()

    def test_bad_token_never_reaches_serve(self, proxy, stub):
        _request("GET", _proxy_url(proxy, "/story/x/brief"), token="nope")
        assert stub.requests == []  # 拒绝不落上游


# ---- 白名单 ----


class TestWhitelist:
    def test_non_whitelist_path_404(self, proxy, stub, log_file):
        status, body = _request("GET", _proxy_url(proxy, "/api/story"))
        assert status == 404
        assert body["refused"] == "not_whitelisted"
        assert stub.requests == []  # 白名单外不落上游
        assert any("upstream=-" in line for line in _read_log(log_file))

    def test_method_not_in_whitelist_404(self, proxy):
        """白名单按 (method, path) 二元组:POST /status、GET advance 都不放行。"""
        status, _ = _request("POST", _proxy_url(proxy, "/status"), body={})
        assert status == 404
        status, _ = _request("GET", _proxy_url(proxy, f"/story/{KEY}/advance"))
        assert status == 404

    def test_match_route_unit(self):
        """纯函数:命中/key URL 解码/method 不符/空 key。"""
        assert match_route("GET", "/status") == ("status", "")
        assert match_route("GET", "/patrol/summary") == ("patrol_summary", "")
        assert match_route("GET", "/story/a%20b/brief") == ("brief", "a b")
        assert match_route("POST", "/story/x/advance") == ("advance", "x")
        assert match_route("POST", "/status") is None
        assert match_route("GET", "/story/x/advance") is None
        assert match_route("GET", "/story//brief") is None
        assert match_route("GET", "/etc/passwd") is None
        assert match_route("GET", "/") is None


# ---- /status ----


class TestStatus:
    def test_status_serve_up(self, proxy, stub, log_file):
        stub.responses[("GET", "/api/story")] = (200, [])
        status, body = _request("GET", _proxy_url(proxy, "/status"))
        assert status == 200
        assert body == {"ok": True, "proxy": "alive", "serve": "up"}
        assert any("upstream=200" in line for line in _read_log(log_file))

    def test_status_serve_down_still_200(self, stub, log_file):
        """serve down 不是代理故障:200 + serve:"down"(隧道心跳语义)。"""
        stub.stop()
        p = _Proxy(stub.base_url, token=TOKEN, log_path=log_file)
        try:
            status, body = _request("GET", f"http://127.0.0.1:{p.port}/status")
            assert status == 200
            assert body == {"ok": True, "proxy": "alive", "serve": "down"}
        finally:
            p.stop()


# ---- brief / patrol 转发透传 ----


class TestForwarding:
    def test_brief_forwards_and_passthrough(self, proxy, stub, log_file):
        detail = _story_detail([])
        stub.responses[("GET", f"/api/story/{KEY}")] = (200, detail)
        status, body = _request("GET", _proxy_url(proxy, f"/story/{KEY}/brief"))
        assert status == 200
        assert body == detail
        assert stub.requests[0]["method"] == "GET"
        assert stub.requests[0]["path"] == f"/api/story/{KEY}"
        assert any(
            f"| GET | /story/{KEY}/brief | token=ok | upstream=200" in line
            for line in _read_log(log_file)
        )

    def test_brief_serve_404_passthrough(self, proxy, stub):
        status, body = _request("GET", _proxy_url(proxy, "/story/ghost/brief"))
        assert status == 404
        assert "stub 无此路由" in body["detail"]  # serve 的应答原样透传

    def test_patrol_summary_forwards_to_story_list(self, proxy, stub):
        """GET /patrol/summary → serve 的列表徽标聚合端点 GET /api/story。"""
        listing = [
            {
                "storyKey": KEY,
                "patrolSummary": {"itemsCount": 3, "latestResult": "FAIL"},
            }
        ]
        stub.responses[("GET", "/api/story")] = (200, listing)
        status, body = _request("GET", _proxy_url(proxy, "/patrol/summary"))
        assert status == 200
        assert body == listing
        assert stub.requests[0]["path"] == "/api/story"

    def test_forward_bypasses_system_proxy(self, proxy, stub, monkeypatch):
        """http_proxy 指向必死代理也影响不到转发(loopback 直连,显式空代理表)。"""
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        stub.responses[("GET", f"/api/story/{KEY}")] = (200, _story_detail([]))
        status, _ = _request("GET", _proxy_url(proxy, f"/story/{KEY}/brief"))
        assert status == 200


# ---- advance:终态执法 + 合法续推 ----


class TestAdvance:
    def test_non_terminal_gate_forwards_with_confirmed_via_wechat(
        self, proxy, stub, log_file
    ):
        """story_state 非终态门:转发 lifecycle/advance 且 body 带 confirmed_via=wechat。"""
        stub.responses[("GET", f"/api/story/{KEY}")] = (
            200,
            _story_detail(
                [
                    {
                        "kind": "story_state",
                        "targetState": "测试",
                        "targetIsTerminal": False,
                    }
                ]
            ),
        )
        stub.responses[("POST", f"/api/story/{KEY}/lifecycle/advance")] = (
            200,
            {"ok": True, "lifecycle_state": "测试", "status": "active"},
        )
        status, body = _request(
            "POST", _proxy_url(proxy, f"/story/{KEY}/advance"), body={"target": "测试"}
        )
        assert status == 200
        assert body == {"ok": True, "lifecycle_state": "测试", "status": "active"}
        posts = [r for r in stub.requests if r["method"] == "POST"]
        assert len(posts) == 1
        assert posts[0]["path"] == f"/api/story/{KEY}/lifecycle/advance"
        assert json.loads(posts[0]["body"])["confirmed_via"] == "wechat"
        assert any("upstream=200" in line for line in _read_log(log_file))

    def test_terminal_gate_403_never_forwarded(self, proxy, stub, log_file):
        """终态门(§8 微信不做终态确认):403 + 中文说明,绝不转发。"""
        stub.responses[("GET", f"/api/story/{KEY}")] = (
            200,
            _story_detail(
                [
                    {
                        "kind": "upgrade",
                        "targetState": "结项",
                        "targetIsTerminal": True,
                    }
                ]
            ),
        )
        status, body = _request(
            "POST", _proxy_url(proxy, f"/story/{KEY}/advance"), body={"target": "结项"}
        )
        assert status == 403
        assert body["refused"] == "terminal_target"
        assert "微信通道不做终态确认" in body["summary"]
        assert body["gate"]["targetState"] == "结项"
        assert not [r for r in stub.requests if r["method"] == "POST"]  # 未转发
        assert any("upstream=-" in line for line in _read_log(log_file))

    def test_no_confirm_gates_passthrough_serve_response(self, proxy, stub):
        """无 confirmGates(无门可推):仍转发,serve 的原响应(409)透传。"""
        stub.responses[("GET", f"/api/story/{KEY}")] = (200, _story_detail([]))
        stub.responses[("POST", f"/api/story/{KEY}/lifecycle/advance")] = (
            409,
            {"detail": "已到终态,无法推进"},
        )
        status, body = _request(
            "POST", _proxy_url(proxy, f"/story/{KEY}/advance"), body={"target": "结项"}
        )
        assert status == 409
        assert body == {"detail": "已到终态,无法推进"}
        posts = [r for r in stub.requests if r["method"] == "POST"]
        assert json.loads(posts[0]["body"])["confirmed_via"] == "wechat"

    def test_advance_bad_json_400(self, proxy, stub):
        req = urllib.request.Request(
            _proxy_url(proxy, f"/story/{KEY}/advance"),
            data=b"{not-json",
            headers={"X-Internal-Token": TOKEN, "Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as resp:
                status = int(resp.status)
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            status, body = int(e.code), json.loads(e.read())
        assert status == 400
        assert body["refused"] == "bad_request"
        assert stub.requests == []

    def test_advance_serve_down_502(self, stub, log_file):
        stub.stop()
        p = _Proxy(stub.base_url, token=TOKEN, log_path=log_file)
        try:
            status, body = _request(
                "POST",
                f"http://127.0.0.1:{p.port}/story/{KEY}/advance",
                body={"target": "测试"},
            )
            assert status == 502
            assert body["refused"] == "serve_unreachable"
            assert "story serve" in body["summary"]
        finally:
            p.stop()


# ---- 请求日志格式 ----


class TestRequestLog:
    def test_log_line_format(self, proxy, stub, log_file):
        stub.responses[("GET", f"/api/story/{KEY}")] = (200, _story_detail([]))
        _request("GET", _proxy_url(proxy, f"/story/{KEY}/brief"))
        lines = _read_log(log_file)
        assert len(lines) == 1
        pattern = (
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}"
            rf" \| GET \| /story/{KEY}/brief \| token=ok \| upstream=200 \| \d+ms$"
        )
        assert re.match(pattern, lines[0]), lines[0]

    def test_append_request_log_direct(self, tmp_path):
        """纯单元:列顺序 = 时间|方法|路径|token|上游状态码|耗时 ms。"""
        path = tmp_path / LOG_FILE_NAME
        append_request_log(path, "POST", "/story/k/advance", "ok", 409, 12)
        line = path.read_text(encoding="utf-8").splitlines()[0]
        cols = line.split(" | ")
        assert len(cols) == 6
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$", cols[0])
        assert cols[1:] == ["POST", "/story/k/advance", "token=ok", "upstream=409", "12ms"]


# ---- 装配与 env ----


class TestAssembly:
    def test_bind_conflict_chinese_error(self):
        """端口被占用 → ButlerProxyBindError,中文报错带端口。"""
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        try:
            with pytest.raises(ButlerProxyBindError) as ei:
                make_server(port=occupied, token="t", log_path="x.log")
            assert str(occupied) in str(ei.value)
            assert "端口" in str(ei.value)
        finally:
            blocker.close()

    def test_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BUTLER_PROXY_PORT", "18999")
        monkeypatch.setenv("STORY_SERVE_URL", "http://127.0.0.1:9999/")
        monkeypatch.setenv("BUTLER_PROXY_TOKEN", "  env-token  ")
        monkeypatch.setenv("STORY_LOG_DIR", str(tmp_path))
        assert proxy_port() == 18999
        assert serve_base_url() == "http://127.0.0.1:9999"  # 尾斜杠归一
        assert proxy_token() == "env-token"  # 去空白
        assert log_file_path() == tmp_path / LOG_FILE_NAME

    def test_log_file_default_repo_ws(self, monkeypatch):
        """无 STORY_LOG_DIR:默认 <repo>/ws/butler-proxy.log(ws/ 已 gitignore)。"""
        monkeypatch.delenv("STORY_LOG_DIR", raising=False)
        path = log_file_path()
        assert path.name == LOG_FILE_NAME
        assert path.parent.name == "ws"
