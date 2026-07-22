"""consult runner —— spawn 外援 CLI + poll advisory 结果文件(同步)。

复用 ``planner.py`` 的 headless spawn 骨架(Popen + stdin 写 prompt + done file 轮询),
但**砍掉** stage 推进 / clarification reset / PTY 回收 / db.update_story 等耦合逻辑,
并把 stdout/stderr 的防死锁方案从 drain 线程改为落日志文件(见下)。

设计原则(DESIGN-consult-tool §5.5):
- 纯同步(consult FC loop 在阻塞调用里调它)
- 零 DB 副作用(只读写文件;DB 事件归 consult_orchestrator / consult_cmd)
- 失败不抛异常外泄,返 ``{"status": ..., "error": ...}`` 让编排 LLM 决策

属于 AGENTS.md 分层的 **Handler** 层:执行 spawn(副作用);不做决策。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from ...infra.json_helpers import robust_json_parse
from ...infra.paths import consult_result_file_rel
from ...knowledge.adapters import get_adapter

_DEFAULT_TIMEOUT = 180
_DEFAULT_POLL_INTERVAL = 5.0
_MAX_SPAWN_ATTEMPTS = 3


def run_consult_sync(
    *,
    adapter_name: str,
    focus: str,
    workspace: str,
    request_id: str,
    model: str = "",
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    max_attempts: int = _MAX_SPAWN_ATTEMPTS,
    # 注入点(测试用)
    popen_fn: Callable = subprocess.Popen,
    sleep_fn: Callable[[float], None] = time.sleep,
    kill_fn: Callable | None = None,
) -> dict:
    """Spawn 外援 CLI + poll 结果文件 → 返 advisory dict。

    Args:
        adapter_name: claude / kimi(codex 无 headless,见 DESIGN §3.6)。
        focus: 给外援的调查指令(2-3 句)。
        workspace: 工作区根。
        request_id: consult 请求 id(uuid hex[:12] + 轮次后缀,由 caller 拼)。
        model: 可选 model 名(透传 adapter.headless_launch_cmd)。
        cwd: spawn 工作目录(缺省 = workspace)。
        env: spawn 环境(缺省 = 继承当前 env)。caller 注入 STORY_CONSULT_DEPTH=1
            在自己拼 env 时;本函数**额外强制**注入一次(防御性,确保递归守卫到位)。
        timeout: 外援最长等待秒数(默认 180)。
        poll_interval: 轮询间隔(默认 5s;测试可缩短)。
        max_attempts: 外援 spawn 失败后重试上限(默认 3)。

    Returns:
        dict,保证字段:
        - status: "ok" | "timeout" | "spawn_failed" | "no_headless"
        - findings: dict(外援的 advisory,可能为空)
        - error: str(失败时的诊断)

        status="ok" 时 findings 含外援写的任意字段(自由 schema)。

    不抛异常 —— 所有失败路径返 ``{"status": ..., "error": ...}``,让编排 LLM 决策。
    """
    try:
        adapter = get_adapter(adapter_name)
    except Exception as exc:
        return {
            "status": "no_headless",
            "findings": {},
            "error": f"adapter {adapter_name!r} not registered: {exc}",
        }
    try:
        launch_cmd = adapter.headless_launch_cmd(model=model, prompt="")
    except Exception as exc:
        return {
            "status": "no_headless",
            "findings": {},
            "error": f"adapter {adapter_name!r} headless_launch_cmd failed: {exc}",
        }
    if launch_cmd is None:
        return {
            "status": "no_headless",
            "findings": {},
            "error": f"adapter {adapter_name!r} has no headless mode",
        }

    result_rel = consult_result_file_rel(request_id)
    result_path = Path(workspace) / result_rel
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "status": "spawn_failed",
            "findings": {},
            "error": f"mkdir failed for {result_path.parent}: {exc}",
        }
    if result_path.exists():
        try:
            result_path.unlink()  # 清旧(防误读)
        except OSError:
            pass
    # 外援 stdout/stderr 落 .log(与 .json 同目录)—— 不用 PIPE。PIPE 不排空会写满
    # 缓冲(Windows ~64KB)阻塞子进程,外援永远写不出结果文件(planner.py 同款坑)。
    log_path = result_path.with_suffix(".log")

    prompt = _build_reviewer_prompt(focus=focus, result_file=result_rel)

    kill = kill_fn or _default_kill
    spawn_cwd = cwd or workspace
    # 外援 env:继承 + 强制注入递归守卫(外援不可再 consult,DESIGN §5.2)
    base_env = env if env is not None else os.environ
    reviewer_env = {**base_env, "STORY_CONSULT_DEPTH": "1"}
    elapsed = 0.0
    attempt = 1
    proc = None
    log_fh = None

    try:
        while elapsed < timeout:
            # spawn(首次或重试)
            if proc is None:
                try:
                    log_fh = open(log_path, "ab")  # noqa: SIM115 - 随 proc 生命周期
                    proc = popen_fn(
                        launch_cmd,
                        cwd=spawn_cwd,
                        stdin=subprocess.PIPE,
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,  # 合并进同一日志
                        env=reviewer_env,
                    )
                    proc.stdin.write(prompt.encode("utf-8"))
                    proc.stdin.close()
                except Exception as exc:
                    _close_quiet(log_fh)
                    log_fh = None
                    proc = None
                    if attempt < max_attempts:
                        attempt += 1
                        sleep_fn(poll_interval)
                        elapsed += poll_interval
                        continue
                    return {
                        "status": "spawn_failed",
                        "findings": {},
                        "error": f"Popen failed after {attempt} attempts: {exc}",
                    }

            # 外援提前退出但没写结果文件 → 重试
            if proc.poll() is not None and not result_path.exists():
                if attempt < max_attempts:
                    attempt += 1
                    kill(proc)
                    _close_quiet(log_fh)
                    log_fh = None
                    proc = None
                    sleep_fn(poll_interval)
                    elapsed += poll_interval
                    continue
                return {
                    "status": "spawn_failed",
                    "findings": {},
                    "error": (
                        f"reviewer exited without writing result (attempt {attempt}); "
                        f"see {log_path}"
                    ),
                }

            # 结果文件出现 → 解析返回
            if result_path.exists():
                try:
                    data = robust_json_parse(result_path) or {}
                    return {"status": "ok", "findings": data, "error": ""}
                except Exception:
                    pass  # 半写文件,下一轮再试

            sleep_fn(poll_interval)
            elapsed += poll_interval

        # 超时
        return {
            "status": "timeout",
            "findings": {},
            "error": f"reviewer did not finish within {timeout}s; see {log_path}",
        }
    finally:
        if proc is not None:
            try:
                kill(proc)
            except Exception:
                pass
        _close_quiet(log_fh)


def _build_reviewer_prompt(*, focus: str, result_file: str) -> str:
    """给外援 CLI 的 prompt —— 调查 focus,把结论写到 result_file。"""
    return f"""## 任务:外援调查(advisory)

你被编排层 spawn 为**外援 reviewer**,协助另一个 code agent 解决问题。你的产出是 **advisory**(建议),不是决策。

### 调查指令
{focus}

### 完成协议
调查完成后,把结论写入文件 `{result_file}`,JSON 格式:
```json
{{
  "summary": "<一句话结论>",
  "findings": ["<具体发现 1>", "<具体发现 2>"],
  "recommendation": "<给请求方 code agent 的建议>",
  "evidence": ["<引用的代码位置 / 文件 / 行号>"],
  "confidence": "low|medium|high"
}}
```

**纪律**:
- 你**不写业务代码**,只调查 + 写 advisory 结果文件。
- 聚焦调查指令,不要发散。
- 引用具体代码位置(file:line),不要泛泛而谈。
- 完成即写文件退出,不要等待。
- 你**不可**再调 `story consult`(递归守卫会拒绝)。遇到不确定,把不确定性写进 findings。
"""


def _default_kill(proc):
    """复用 planner._kill_headless 的 taskkill /T 逻辑(Windows 进程树)。

    DESGIN §6.2 选项 A:第一版直接 import 同包 planner 的 _kill_headless,
    不下沉到 infra(避免改动面扩散)。planner._kill_headless 是 Windows taskkill /T
    杀整进程树(子进程 node runtime 也要回收,否则孤儿)。
    """
    from .planner import _kill_headless

    _kill_headless(proc)


def _close_quiet(fh):
    """best-effort 关闭日志文件句柄(已 None / 已关 / 异常都吞掉)。"""
    if fh is None:
        return
    try:
        fh.close()
    except Exception:
        pass


__all__ = ["run_consult_sync"]
