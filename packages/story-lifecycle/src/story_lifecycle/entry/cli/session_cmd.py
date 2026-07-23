"""story session —— code agent 的会话 id 回写通道(CLI 一次性进程)。

半自动流程:用户复制 release-prompt → 粘进自己开的 claude/kimi 终端 → agent 跑完
任务后用 Bash 调 ``story session --writeback <id>`` 把自己的会话 id 写回 DB →
前端「复制 resume 文案」按钮能读到 → 下次用户复制 ``claude --resume <id>`` /
``kimi -S <id>`` 续上,省 token。

env(由 release-prompt 指令让 agent 自己设/继承):
- STORY_KEY / STORY_STAGE / STORY_WORKSPACE / STORY_ADAPTER —— 标识回写哪条 session。

与 consult_cmd 同构:纯核心 + click 薄壳,best-effort 落 DB,exit 0 不阻塞。
"""

from __future__ import annotations

import click


@click.command("session")
@click.option(
    "--writeback",
    "session_id",
    default="",
    help="回写当前会话 id(从 claude --resume / kimi -S 的 transcript 或环境拿到)",
)
def session_cmd(session_id: str):
    """把 code agent 的会话 id 写回后端(半自动 resume 闭环)。"""
    import os

    if not session_id:
        click.echo("session: 需要 --writeback <id>(你的 claude/kimi 会话 id)。")
        raise SystemExit(2)

    story_key = os.environ.get("STORY_KEY", "")
    if not story_key:
        click.echo("session: 缺 STORY_KEY —— 只能在 story 上下文里调用(提示词已设)。")
        raise SystemExit(2)

    stage = os.environ.get("STORY_STAGE", "") or "design"
    adapter = os.environ.get("STORY_ADAPTER", "") or "claude"

    try:
        from ...infra.db import models as db

        db.upsert_session(story_key, stage, adapter, session_id=session_id)
        db.log_event(
            story_key,
            stage,
            "session_writeback",
            {"adapter": adapter, "session_id": session_id},
        )
    except Exception as exc:
        # best-effort:回写失败不阻塞 agent(它已跑完任务),只提示。
        click.echo(f"session: 回写失败({exc}),不影响你的任务结果。")
        raise SystemExit(0)

    click.echo(f"session: 已回写 {adapter}/{stage} → {session_id}")


__all__ = ["session_cmd"]
