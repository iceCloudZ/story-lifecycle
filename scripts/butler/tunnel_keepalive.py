#!/usr/bin/env python3
"""管家桌面栈保活器(WP5,DESIGN-story-butler §3.5)。

职责三件事,单实例(锁端口 18182):
1. **代理常驻** —— 令牌代理(butler_proxy,127.0.0.1:18181)不在就拉起(DETACHED,
   不随本进程死);
2. **隧道常驻** —— 反向隧道(受限 key,101 的 localhost:18180 → 桌面 18181)
   不在就重建。**心跳即真相**:不追踪 ssh 进程归属(孤儿隧道同样是好隧道),
   以「从 101 经隧道口 curl /status 成功」为唯一健康判据,探活失败才重建;
3. **心跳告警** —— 端到端探活连续 3 次失败 → 经出方向 remind.py 推微信
   「管家隧道异常」(30 分钟限频;告警链路本身失败则记本地日志待恢复后重试)。

配置:同目录 ``butler.env``(已 gitignore,模板见 ``butler.env.example``):
  BUTLER_PROXY_TOKEN   必填,与 101 侧 clawbot 的 token 一致
  BUTLER_SSH_HOST      默认 101(心跳/告警用的出方向 ssh,主 key)
  HEARTBEAT_INTERVAL   秒,默认 300

部署:用户启动文件夹的 ``StoryButlerTunnel.vbs``(免提权)或计划任务(需管理员,
二者等价)登录时拉起本脚本(pythonw 无窗)。
日志:``ws/butler-keepalive.log``。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS = REPO / "ws"
ENV_FILE = Path(__file__).resolve().parent / "butler.env"
LOG_FILE = WS / "butler-keepalive.log"

LOCK_PORT = 18182
PROXY_PORT = 18181
TUNNEL_KEY = Path.home() / ".ssh" / "id_butler_tunnel"
REMOTE_LISTEN = 18180  # 101 侧隧道口
HEARTBEAT_INTERVAL = 300
HEARTBEAT_FAILS_ALERT = 3
ALERT_RATELIMIT_S = 30 * 60
RECHECK_INTERVAL = 15
TZ = datetime.now().astimezone().tzinfo


def log(msg: str) -> None:
    line = f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        WS.mkdir(exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def take_lock() -> bool:
    """单实例锁:绑 18182。失败 = 已有实例在跑,本进程退出。"""
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", LOCK_PORT))
        return True
    except OSError:
        return False


def spawn_proxy(token: str) -> None:
    """拉起令牌代理(DETACHED:不随本进程死,计划任务重启不误杀)。"""
    py = REPO / ".venv-monorepo-test" / "Scripts" / "python.exe"
    env = {**os.environ, "BUTLER_PROXY_TOKEN": token, "BUTLER_PROXY_PORT": str(PROXY_PORT)}
    flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [str(py), "-m", "story_lifecycle.butler_proxy"],
            cwd=str(REPO), env=env, creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        log(f"proxy spawned on :{PROXY_PORT}")
    except Exception as e:
        log(f"proxy spawn FAILED: {e}")


def spawn_tunnel(host: str) -> None:
    """重建反向隧道(受限 key;ExitOnForwardFailure:101 侧 18180 被占(孤儿隧道在)即退)。"""
    if not TUNNEL_KEY.exists():
        log(f"tunnel key missing: {TUNNEL_KEY}")
        return
    try:
        subprocess.Popen(
            ["ssh", "-i", str(TUNNEL_KEY), "-N",
             f"-R", f"{REMOTE_LISTEN}:127.0.0.1:{PROXY_PORT}",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
             "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes", host],
            creationflags=0x08000000,  # CREATE_NO_WINDOW(随本进程生命周期即可)
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        log(f"tunnel spawned (101:{REMOTE_LISTEN} -> desktop:{PROXY_PORT})")
    except Exception as e:
        log(f"tunnel spawn FAILED: {e}")


def heartbeat(token: str, host: str) -> bool:
    """端到端探活:出方向 ssh(主 key)到 101,经隧道口 curl /status。"""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
             f'curl -s -m 8 -H "X-Internal-Token: {token}" '
             f"http://127.0.0.1:{REMOTE_LISTEN}/status"],
            capture_output=True, text=True, timeout=25,
        )
        return '"ok"' in (r.stdout or "")
    except Exception:
        return False


def alert(token: str, host: str, text: str, state: dict) -> None:
    """告警走 remind.py(与隧道无关的出方向链路);30 分钟限频。"""
    now = time.time()
    if now - state.get("last_alert", 0) < ALERT_RATELIMIT_S:
        return
    state["last_alert"] = now
    try:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
             f"/home/ubuntu/clawbot-inbox/venv/bin/python "
             f"/home/ubuntu/clawbot-inbox/remind.py {text!r}"],
            capture_output=True, text=True, timeout=40,
        )
        log(f"alert sent: {text}")
    except Exception as e:
        log(f"alert FAILED (will retry after rate-limit): {e}")


def main() -> int:
    if not take_lock():
        print("another keepalive instance holds the lock, exit", flush=True)
        return 0
    cfg = load_env()
    token = cfg.get("BUTLER_PROXY_TOKEN", "")
    host = cfg.get("BUTLER_SSH_HOST", "101")
    hb_interval = int(cfg.get("HEARTBEAT_INTERVAL", str(HEARTBEAT_INTERVAL)))
    if not token:
        log("FATAL: butler.env 缺 BUTLER_PROXY_TOKEN,代理起不来(安全默认拒绝一切)——退出")
        return 1
    log(f"keepalive start (host={host}, hb={hb_interval}s)")

    state = {"last_hb": 0.0, "fails": 0, "last_alert": 0.0}
    while True:
        try:
            if not port_open(PROXY_PORT):
                spawn_proxy(token)
            if time.time() - state["last_hb"] >= hb_interval:
                state["last_hb"] = time.time()
                if heartbeat(token, host):
                    if state["fails"]:
                        log("heartbeat recovered")
                    state["fails"] = 0
                else:
                    state["fails"] += 1
                    log(f"heartbeat fail x{state['fails']}")
                    # 探活失败即尝试重建(孤儿占用时 ExitOnForwardFailure 会退,
                    # 下一轮心跳仍失败则继续告警+重试)
                    spawn_tunnel(host)
                    if state["fails"] >= HEARTBEAT_FAILS_ALERT:
                        alert(token, host,
                              "管家桌面栈告警:反向隧道心跳连续失败 "
                              f"{state['fails']} 次,微信指令通道可能不可用(推送不受影响)",
                              state)
        except Exception as e:
            log(f"loop error (non-fatal): {e}")
        time.sleep(RECHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
