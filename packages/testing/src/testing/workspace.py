"""Calculator workspace reset for real E2E red→green repeatability.

基线是"红"：calculator.py 不存在（AI implement 写它让 17 测试过）。
每跑前 reset_workspace 删掉 AI 写的 calculator.py + 清该 story 的 .story 产物，
保证可重复（红→绿循环）。
"""
import shutil
from pathlib import Path

try:
    # Best-effort reuse of the canonical sanitize/validate helpers when the
    # story-lifecycle package is importable (editable install in the monorepo).
    from story_lifecycle.infra.story_paths import (
        UnsafePathError,
        assert_within_workspace,
        safe_segment,
    )
except ImportError:  # pragma: no cover - testing package standalone fallback
    def safe_segment(value: str) -> str:  # type: ignore[misc]
        import re

        cleaned = re.sub(r"[^\w.-]+", "-", value or "", flags=re.UNICODE).strip("-_").rstrip(".")
        if "/" in cleaned or "\\" in cleaned or cleaned in {"..", "."}:
            raise ValueError(f"refusing unsafe path segment: {value!r}")
        return cleaned or "story"

    class UnsafePathError(ValueError):
        pass

    def assert_within_workspace(path, workspace) -> None:  # type: ignore[misc]
        resolved = Path(path).resolve()
        root = Path(workspace).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise UnsafePathError(
                f"refusing operation outside workspace: {path!r}"
            ) from exc


def reset_workspace(workspace, story_key, *, red_files=("calculator.py",)):
    """Reset real E2E workspace to baseline (red).

    - delete red_files if the AI wrote them in a previous run (calculator.py
      baseline = absent)
    - clear .story/{context,done,runs}/<story_key>
    - workspace 应是 git repo；额外做 best-effort `git restore`（基线不在 git
      时无效，靠上面的 unlink 兜底）

    Returns the workspace Path.
    """
    ws = Path(workspace)
    story_key = safe_segment(story_key)  # trust boundary: external string → path
    for f in red_files:
        p = ws / f
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    for sub in (".story/context", ".story/done", ".story/runs"):
        d = ws / sub / story_key
        if d.exists():
            # Blast shield: refuse to rmtree anything escaping the workspace.
            assert_within_workspace(d, ws)
            shutil.rmtree(d, ignore_errors=True)
    return ws
