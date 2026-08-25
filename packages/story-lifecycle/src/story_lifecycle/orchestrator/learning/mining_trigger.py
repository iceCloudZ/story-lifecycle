"""M2 (B2) — story 完成后的增量挖掘触发器（DESIGN-knowledge-flywheel-closure §4.2）。

挂载点（Handler 层 —— AGENTS.md 硬规则：只有 Handler 可起线程/子进程）：
  - ``handlers._handle_all_stages_done``（driver 同步 shim 路径）
  - ``scheduler._complete_story``（编排线程路径）
  两处都在 ``_persist_playbook_for_story`` 之后调 ``maybe_trigger_incremental``。

干什么：后台 daemon 线程顺序跑三个子进程（cwd = story-miner 包根）：
  ``python -m miner.story_ingest`` → ``python -m miner.link`` → ``python scripts/generate_playbooks.py``
  不跑 store 全量扫 / refresh.sh full 档重活（那些保持手动/外部 cron）。

防抖（满足其一即真跑）：距上次真跑 ≥ ``_MIN_INTERVAL_SEC``(30min)，或距上次真跑
累计 ≥ ``_MIN_STORIES``(3) 个 story 完成 —— 低频环境不被计数饿死，高频不被打爆。

单飞：同进程至多一个挖掘线程，在跑则跳过（完成计数照常累计，worker 结束后按
计数条件自然补跑）。

软 seam（对齐 BaseStoryContextProvider 的 lenient 哲学）：
  - ``miner`` 不可导入（story-lifecycle 独立安装运行）→ 整体 no-op；
  - 任何子步骤非零退出/超时 → warning 落日志即止，绝不影响 story 完成路径。

Anti-pattern（§4.6）：不许从 Decider（``reflection.reflect``）触发挖掘 —— 副作用只属于 Handler。
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("story-lifecycle.mining-trigger")

_MIN_INTERVAL_SEC = 30 * 60  # 距上次真跑 ≥30min → 可跑
_MIN_STORIES = 3  # 或距上次真跑累计 ≥3 个完成 → 可跑
_STEP_TIMEOUT_SEC = 10 * 60  # 单步子进程上限（story_ingest 扫 transcript 较慢，给足）

_state_lock = threading.Lock()
_last_run_ts: float | None = None  # 上次真跑启动时刻；None=从未跑过
_completed_since_run = 0  # 距上次真跑累计的 story 完成数
_worker_alive = False  # 单飞标记


def maybe_trigger_incremental(story_key: str) -> bool:
    """story 完成钩子：判断是否启动增量挖掘。返回是否真的启动了（测试/日志用）。

    任何失败路径都返回 False 且不留副作用——tagging/mining 全是 best-effort，
    不得阻塞 story 完成（§4.6 不变量）。
    """
    global _last_run_ts, _completed_since_run, _worker_alive

    if not _miner_available():
        return False

    with _state_lock:
        _completed_since_run += 1
        if _worker_alive:
            return False
        interval_ok = (
            _last_run_ts is None or (_now() - _last_run_ts) >= _MIN_INTERVAL_SEC
        )
        count_ok = _completed_since_run >= _MIN_STORIES
        if not (interval_ok or count_ok):
            return False
        _worker_alive = True
        _last_run_ts = _now()
        _completed_since_run = 0

    _launch_worker(story_key)
    return True


# ---- 内部：单测的注入点（monkeypatch 这里，不起真线程/子进程） ----


def _now() -> float:
    return time.time()


def _launch_worker(story_key: str) -> None:
    threading.Thread(
        target=_run_pipeline,
        args=(story_key,),
        daemon=True,
        name=f"mining-incremental-{story_key}",
    ).start()


def _worker_finished() -> None:
    global _worker_alive
    with _state_lock:
        _worker_alive = False


def _miner_available() -> bool:
    # find_spec 不执行包代码；miner 未安装（lifecycle 独立运行）→ no-op。
    try:
        return importlib.util.find_spec("miner") is not None
    except (ImportError, ValueError):
        return False


def _miner_root() -> Path | None:
    try:
        import miner

        return Path(miner.__file__).resolve().parents[1]
    except Exception:  # noqa: BLE001 — 软 seam，任何失败都退化为 skip
        return None


def _run_pipeline(story_key: str) -> None:
    """顺序跑增量挖掘三步；任一步失败即停（后面步骤依赖前面产物）。"""
    try:
        root = _miner_root()
        if root is None:
            log.warning("[mining] miner root unresolved — skip incremental mining")
            return
        steps = (
            [sys.executable, "-m", "miner.story_ingest"],
            [sys.executable, "-m", "miner.link"],
            [sys.executable, "scripts/generate_playbooks.py"],
        )
        for cmd in steps:
            label = " ".join(cmd[1:])
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_STEP_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                log.warning(
                    "[mining] step timeout (%.0fs): %s", _STEP_TIMEOUT_SEC, label
                )
                return
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-500:]
                log.warning(
                    "[mining] step failed rc=%s: %s — %s", proc.returncode, label, tail
                )
                return
            log.info("[mining] step ok: %s", label)
        log.info("[mining] incremental pipeline done (triggered by %s)", story_key)
    except Exception:  # noqa: BLE001 — 挖掘绝不影响 story 完成路径
        log.exception("[mining] incremental pipeline crashed")
    finally:
        _worker_finished()


def _reset_state() -> None:
    """测试专用：清空防抖/单飞状态。"""
    global _last_run_ts, _completed_since_run, _worker_alive
    with _state_lock:
        _last_run_ts = None
        _completed_since_run = 0
        _worker_alive = False
