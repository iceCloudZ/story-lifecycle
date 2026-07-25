"""atomic_write — 原子文件写(tmp → fsync → rename)。

DESIGN-artifact-driven-stage-completion §2.3 / STEP 1 子任务 1.3。

业界标准做法:写临时文件(同目录,保证同文件系统)→ fsync → rename。POSIX rename
保证原子;Windows 用 MoveFileEx(MOVEFILE_REPLACE_EXISTING)大部分原子,杀软占用需重试。

**红线**(评审硬伤):原子写必须和"文件存在检查"同批做 —— story-tool declare 单次
调用内完成原子写 + 版本化 + done.json 兼容视图,planner 的 check_artifacts_landed
查的是这里写出的文件,无半成品竞态窗口。

失败降级(设计 §7.5):rename 重试到上限仍失败 → 直接写并标 atomic=false,人确认兜底。
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from pathlib import Path

log = logging.getLogger("story-lifecycle.atomic_write")

# rename 重试配置:杀软瞬时占用目标文件时退避重试。3 次,退避 0.2/0.5/1.0s。
_RETRY_DELAYS = (0.2, 0.5, 1.0)


def _fsync_dir(path: Path) -> None:
    """best-effort fsync 父目录(rename 元数据落盘)。POSIX only;Windows no-op。"""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # 某些文件系统不支持目录 fsync,非致命


def _replace_with_movefileex(src: Path, dst: Path) -> None:
    """Windows:用 MoveFileEx + MOVEFILE_REPLACE_EXISTING 原子替换。

    os.replace 在 Windows 内部用 ReplaceFile,对"目标不存在"和"被杀软锁定"场景不如
    MoveFileEx 稳。MoveFileEx 是 Win32 原子的跨文件名替换(MSDN 保证)。
    """
    import ctypes
    from ctypes import wintypes

    MOVEFILE_REPLACE_EXISTING = 0x1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    MoveFileEx = kernel32.MoveFileExW
    MoveFileEx.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    MoveFileEx.restype = wintypes.BOOL

    ok = MoveFileEx(str(src), str(dst), MOVEFILE_REPLACE_EXISTING)
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(f"MoveFileEx failed (WinError {err}): {src} -> {dst}")


def atomic_write(path: str | Path, content: str | bytes) -> dict:
    """原子写文件:tmp(同目录)→ fsync → rename。

    Returns:
        {"atomic": bool, "path": str, "tmp": str | None}
        - atomic=True:走 tmp+rename 成功(读者要么看到旧版要么看到新版,看不到半成品)
        - atomic=False:rename 重试到上限仍失败,降级直接写(path 内容已写入但中间态可见,
          人确认兜底)。

    纯函数(无 DB / 无 LLM)。content 既支持 str 也支持 bytes(二进制成果物)。
    """
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)

    is_bytes = isinstance(content, bytes)
    # tmp 文件放同目录(保证同文件系统 → rename 原子),唯一名防并发撞。
    tmp = final.with_name(f".{final.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # 1. 写 tmp
        if is_bytes:
            tmp.write_bytes(content)
        else:
            tmp.write_text(content, encoding="utf-8")
        # 2. fsync tmp 内容(保证 rename 后内容已落盘,不丢)
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        # 3. rename tmp → final(重试杀软占用)
        last_exc = None
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                if sys.platform == "win32":
                    _replace_with_movefileex(tmp, final)
                else:
                    os.replace(tmp, final)
                _fsync_dir(final.parent)
                return {"atomic": True, "path": str(final), "tmp": None}
            except OSError as exc:
                last_exc = exc
                log.debug(
                    "atomic rename attempt %d failed: %s -> %s (%s); retrying in %.1fs",
                    attempt + 1,
                    tmp,
                    final,
                    exc,
                    delay,
                )
                time.sleep(delay)
        # 4. 降级:rename 全失败 → 直接写 final(非原子,中间态可见)。标 atomic=false。
        log.warning(
            "atomic rename exhausted retries, falling back to direct write "
            "(atomic=false): %s (last error: %s)",
            final,
            last_exc,
        )
        try:
            tmp.unlink()  # 清残留 tmp
        except OSError:
            pass
        if is_bytes:
            final.write_bytes(content)
        else:
            final.write_text(content, encoding="utf-8")
        return {"atomic": False, "path": str(final), "tmp": None}
    except Exception:
        # 任何异常都清 tmp(不留半成品残留)再抛
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
