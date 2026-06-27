"""Calculator workspace reset for real E2E red→green repeatability.

基线是"红"：calculator.py 不存在（AI implement 写它让 17 测试过）。
每跑前 reset_workspace 删掉 AI 写的 calculator.py + 清该 story 的 .story 产物，
保证可重复（红→绿循环）。
"""
import shutil
from pathlib import Path


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
            shutil.rmtree(d, ignore_errors=True)
    return ws
