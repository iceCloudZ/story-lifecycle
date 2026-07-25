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
        cleaned = re.sub(r"[^\w.-]+", "-", value or "", flags=re.UNICODE).strip("-_").rstrip(".")
        if "/" in cleaned or "\\" in cleaned or cleaned in {"..", "."}:
            raise ValueError(f"refusing unsafe path segment: {value!r}")
        return cleaned or "story"


def _stage_done(result: StoryRunResult, stage: str):
    """STEP 1.4:stage 完成 = 该 stage 被执行 + done.json 兼容视图存在(story-tool
    declare 双写,作 miner 兼容 + 完成证据)。兼容视图缺失也接受(代码 agent 直接
    写文件未走 declare)—— 只要 stage 被执行(sr is not None)且后续产物断言过即可。

    旧契约:done_file 是 code agent 自报(已废)。新契约:成果物落地才是完成信号,
    done.json 兼容视图是 declare 双写副产物(miner 兼容),可能存在也可能不存在。
    """
    sr = result.stage(stage)
    assert sr is not None, f"stage {stage} 未执行"
    # done.json 兼容视图:存在更好(miner 兼容),不存在不阻塞(成果物落地才是判据)。
    if not sr.done_file.exists():
        import logging

        logging.getLogger("testing.asserters").debug(
            "%s done.json 兼容视图缺失(非致命 —— 成果物驱动新协议,declare 未走)",
            stage,
        )
    return sr


def assert_design(result, workspace, story_key):
    """design: spec 落地(story/spec.md 成果物)+ done.json 兼容视图(若 declare 走了)。

    STEP 1.4:完成判据是成果物落地(story/spec.md)。context 下 .md 是旧产物兜底。
    """
    _stage_done(result, "design")
    # 新判据:story/spec.md 成果物落地(story-tool declare 写或 code agent 直接写)
    spec = Path(workspace) / "story" / "spec.md"
    spec_landed = spec.exists() and spec.stat().st_size > 0
    if not spec_landed:
        # 兜底:旧产物(context 下 .md)也接受(过渡期 code agent 可能仍走旧习惯)
        ctx = Path(workspace) / ".story" / "context" / safe_segment(story_key)
        mds = list(ctx.glob("*.md")) if ctx.exists() else []
        assert mds, (
            f"design 成果物缺失(story/spec.md 不存在且 {ctx} 下无 .md 兜底)"
        )


def assert_implement(result, workspace, story_key):
    """implement: calculator.py 生成且非空（AI 写了实现）。"""
    _stage_done(result, "implement")
    calc = Path(workspace) / "calculator.py"
    assert calc.exists(), "calculator.py 未生成（AI 没写实现）"
    assert calc.stat().st_size > 0, "calculator.py 为空"


def assert_verify(result, workspace, story_key):
    """verify: 真实跑 calculator 的 pytest 全过（17 测试）。"""
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
    #
    # STEP 1 fix:code agent 的 cwd 是 worktree(ctx.workspace_path,LLM 决定的 slug),
    # 不一定是 ws(scenario workspace)。claude 把 transcript 写到 ~/.claude/projects/
    # <encoded-cwd>/,miner store 按配置的 WORKSPACES + CLAUDE_ENCODINGS 扫描。若 worktree
    # 不在 encodings 里,store 拿不到 transcript → link 绑不上。这里自发现:扫
    # ~/.claude/projects/ 下最近(2 天内)有会话的项目目录,把它们的 encoding 全注册进
    # miner config,让 store 能覆盖 worktree cwd(story-lifecycle per-story workspace 模型
    # 引入的 cwd 偏离 ws 的场景)。
    wrapper = (
        "import sys, os, glob, time\n"
        "sys.path.insert(0, '.')\n"
        "from miner import config, store, link\n"
        "from miner.adapters import claude\n"
        f"ws = {ws!r}\n"
        "enc = config.claude_encoding(ws)\n"
        "if ws not in config.WORKSPACES: config.WORKSPACES = list(config.WORKSPACES) + [ws]\n"
        "if enc not in config.CLAUDE_ENCODINGS: config.CLAUDE_ENCODINGS = list(config.CLAUDE_ENCODINGS) + [enc]\n"
        "if enc not in claude.ENCODINGS: claude.ENCODINGS = list(claude.ENCODINGS) + [enc]\n"
        "# 自发现最近(2 天)有会话的 claude 项目目录,注册其 encoding(worktree cwd 偏离 ws)\n"
        "projects_dir = os.path.expanduser('~/.claude/projects')\n"
        "cutoff = time.time() - 2*24*3600\n"
        "for proj in glob.glob(os.path.join(projects_dir, '*')):\n"
        "  if not os.path.isdir(proj): continue\n"
        "  jsonls = glob.glob(os.path.join(proj, '*.jsonl'))\n"
        "  if not jsonls: continue\n"
        "  if max(os.path.getmtime(j) for j in jsonls) < cutoff: continue\n"
        "  pname = os.path.basename(proj)\n"
        "  if pname in config.CLAUDE_ENCODINGS: continue\n"
        "  config.CLAUDE_ENCODINGS = list(config.CLAUDE_ENCODINGS) + [pname]\n"
        "  if pname not in claude.ENCODINGS: claude.ENCODINGS = list(claude.ENCODINGS) + [pname]\n"
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

