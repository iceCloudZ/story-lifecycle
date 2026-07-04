"""Per-stage artifact assertions for real E2E.

每个 assert_* 用 harness 的 StoryRunResult.stage(name) + workspace 产物做结构性断言。
AI 输出非确定，故断言"产物存在/非空/测试过"，不锁死内容。
"""
import sqlite3
import subprocess
from pathlib import Path

from testing.harness import StoryRunResult

try:
    # Best-effort reuse of the canonical sanitize helper when the
    # story-lifecycle package is importable (editable install in the monorepo).
    from story_lifecycle.infra.story_paths import safe_segment
except ImportError:  # pragma: no cover - testing package standalone fallback
    import re

    def safe_segment(value: str) -> str:  # type: ignore[misc]
        cleaned = re.sub(r"[^\w.-]+", "-", value or "", flags=re.UNICODE).strip("-._")
        if "/" in cleaned or "\\" in cleaned or cleaned in {"..", "."}:
            raise ValueError(f"refusing unsafe path segment: {value!r}")
        return cleaned or "story"


def _stage_done(result: StoryRunResult, stage: str):
    sr = result.stage(stage)
    assert sr is not None, f"stage {stage} 未执行"
    assert sr.done_file.exists(), f"{stage} done_file 缺失: {sr.done_file}"
    return sr


def assert_design(result, workspace, story_key):
    """design: done_file + context 下有 spec/research 类 .md 产物。"""
    _stage_done(result, "design")
    ctx = Path(workspace) / ".story" / "context" / safe_segment(story_key)
    mds = list(ctx.glob("*.md")) if ctx.exists() else []
    assert mds, f"design 产物缺失（{ctx} 下无 .md）"


def assert_implement(result, workspace, story_key):
    """implement: done_file + calculator.py 生成且非空（AI 写了实现）。"""
    _stage_done(result, "implement")
    calc = Path(workspace) / "calculator.py"
    assert calc.exists(), "calculator.py 未生成（AI 没写实现）"
    assert calc.stat().st_size > 0, "calculator.py 为空"


def assert_verify(result, workspace, story_key):
    """verify: done_file + 真实跑 calculator 的 pytest 全过（17 测试）。"""
    _stage_done(result, "verify")
    r = subprocess.run(
        ["python", "-m", "pytest", str(Path(workspace) / "tests"), "-q"],
        cwd=str(workspace), capture_output=True,
    )
    out = (r.stdout or b"").decode("utf-8", "ignore")
    assert r.returncode == 0, f"calculator pytest 失败 exit {r.returncode}:\n{out[-800:]}"


def assert_done_retrospect(workspace, story_key):
    """done: retrospect.md 生成且非空。"""
    retro = Path(workspace) / ".story" / "done" / safe_segment(story_key) / "retrospect.md"
    assert retro.exists(), f"retrospect.md 缺失: {retro}"
    assert retro.stat().st_size > 0, f"retrospect.md 为空: {retro}"


def assert_miner_linked(db_path, story_key):
    """miner 联动: transcripts.db 有该 story 绑定的 session（story_id high）。"""
    c = sqlite3.connect(str(db_path))
    try:
        n = c.execute(
            "SELECT Count(*) FROM sessions WHERE story_id LIKE ?",
            (f"%{story_key}%",),
        ).fetchone()[0]
    finally:
        c.close()
    assert n > 0, f"miner 未绑定该 story session (story_id LIKE %{story_key}%)"


def run_miner_loopback(workspace):
    """跑真实 miner ingest+link 流水线，作用域限定到 ``workspace``。

    模拟 cron refresh（miner.store + miner.link）：把刚跑完的 AI 会话 transcript
    入库，并通过 story-lifecycle 在 headless 启动时写的 anchor 把 session 绑回
    story（sessions.story_id）。miner 默认只扫 config.json 里的真实工作区，wrapper
    把 ``workspace`` 临时加进 config（子进程内），让流水线覆盖测试场景。

    实现要点：在一个**全新子进程**里跑，cwd=packages/story-miner，使 ``import miner``
    解析到 monorepo 副本——legacy 的 agent-transcript-miner 仍 editable-installed，
    在测试进程里通过 meta_path finder 遮蔽了 monorepo（同包名 ``miner``），in-process
    的 sys.path.insert / sys.modules 清缓存都压不过它；子进程干净导入即可绕开。

    best-effort：失败只告警（miner 是独立关注点；失败会以 assert_miner_linked
    断言失败的形式暴露，而不是崩掉整个 test run）。
    """
    import logging
    import os
    import subprocess
    import sys

    log = logging.getLogger("testing.asserters")
    _miner_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "story-miner")
    )
    if not os.path.isdir(_miner_root):
        log.warning("miner loopback: story-miner not found at %s", _miner_root)
        return
    ws = os.path.normpath(str(workspace))
    # 子进程内：cwd 上 sys.path[0] → import miner 拿到 monorepo；进程内把 ws 加进
    # config（不动 config.json 文件），再跑 store(--since-days 2) + link()。
    wrapper = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from miner import config, store, link\n"
        "from miner.adapters import claude\n"
        f"ws = {ws!r}\n"
        "enc = config.claude_encoding(ws)\n"
        "if ws not in config.WORKSPACES: config.WORKSPACES = list(config.WORKSPACES) + [ws]\n"
        "if enc not in config.CLAUDE_ENCODINGS: config.CLAUDE_ENCODINGS = list(config.CLAUDE_ENCODINGS) + [enc]\n"
        "if enc not in claude.ENCODINGS: claude.ENCODINGS = list(claude.ENCODINGS) + [enc]\n"
        "print('miner loopback subprocess using:', store.__file__)\n"
        "store.main(['--since-days', '2'])\n"
        "link.link()\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [_miner_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", wrapper],
            cwd=_miner_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            log.warning(
                "miner loopback subprocess rc=%d; stdout=%r stderr=%r",
                r.returncode, out[-400:], err[-400:],
            )
        else:
            log.info("miner loopback: %s", out.splitlines()[-1] if out else "done")
    except Exception as exc:
        log.warning("miner loopback subprocess failed: %s", exc)

