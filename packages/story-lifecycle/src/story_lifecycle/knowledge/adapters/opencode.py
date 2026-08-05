"""OpenCode adapter (sst/opencode CLI)."""

import logging
import os
import sqlite3
from pathlib import Path

from .base import BaseAdapter, SessionSpec
from ...infra.terminal.platform_ops import resolve_executable

log = logging.getLogger(__name__)


def _opencode_data_dir() -> Path:
    """opencode 的 per-user 数据根目录。

    opencode 1.18+ 把全部会话数据存在单个 SQLite 文件 ``<data>/opencode.db``
    (表 session/message/part/project;旧版三层 JSON 文件布局已废弃)。根目录随
    平台不同(对应 opencode 的 ``Global.Path.data``):

      - Linux:   ``~/.local/share/opencode``
      - macOS:   ``~/Library/Application Support/opencode``
      - Windows: ``~/.local/share/opencode``  ← 实测确认(opencode 在 Win 上仍用
                   Linux 风格路径,不是 %LOCALAPPDATA%)

    可被 env ``OPENCODE_DATA_DIR`` 覆盖。见 DESIGN-session-pty-id-model.md §2.5。
    """
    override = os.environ.get("OPENCODE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if _sys_darwin():
        return Path.home() / "Library" / "Application Support" / "opencode"
    # Linux + Windows 实测都用 ~/.local/share/opencode。
    return Path.home() / ".local" / "share" / "opencode"


def _opencode_db_path() -> Path:
    return _opencode_data_dir() / "opencode.db"


def _sys_darwin() -> bool:
    import platform

    return platform.system() == "Darwin"


class OpencodeAdapter(BaseAdapter):
    """Adapter for OpenCode CLI (opencode).

    与 claude 同属「prompt baked into command」一族:opencode ``--prompt "..."``
    在 TUI 就绪后自动提交(opencode 源码 home.tsx 证实 args.prompt 有 sent 守卫),
    故 spawner 不做 PTY 注入 / readiness 猜测(``pty_prompt=""``,
    ``readiness_marker=None``)。opencode 的 TUI banner 是 ASCII art,无稳定文本
    可锚定 → 走 baked-in 正好绕开这个坑。

    sid 模型 = 「CLI 自分配、SQLite 查询捕获」:opencode 不支持预指定 session id,
    终端也不吐 sid;会话存进 ``<data>/opencode.db`` 的 ``session`` 表(1.18+ 改用
    SQLite,不再写三层 JSON 文件)。capture_sid_post_exit 在 clean_exit 后查该表,
    按 ``directory = cwd AND time_created >= since_ts`` 取最新 session.id 回填。
    详见 AGENTS.md「Session-id model」与 DESIGN §2.5。
    """

    name = "opencode"

    # opencode 默认模型（spawn 端点 req.model 为空时用）。
    default_model = "opencode-go/deepseek-v4-flash"

    # 见 start_session —— opencode --prompt 自管理 readiness。
    readiness_marker = None
    # CLI 自分配 sid(ses_…),须文件扫描捕获。
    prespecified_session_id = False

    def switch_provider(self, provider: str) -> str | None:
        return None

    def launch_cmd(self, model: str) -> str:
        # 基础启动命令;model/--auto/--prompt 由 interactive_launch_cmd 拼装。
        return "opencode"

    def interactive_launch_cmd(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> list[str]:
        # opencode --prompt "..." —— TUI 就绪后自动提交(源码 home.tsx 的 sent 守卫)。
        #   NEW    → opencode --model <m> --auto --prompt "<seed>"
        #   RESUME → opencode --session <sid> --model <m> --auto --prompt "<continue>"
        # --auto:自动批准未显式 deny 的权限(非完全 bypass,deny 规则仍生效)。
        cmd = [resolve_executable("opencode")]
        if resume and session_id:
            cmd += ["--session", session_id]
        if model:
            cmd += ["--model", model]
        cmd += ["--auto"]
        if prompt:
            cmd += ["--prompt", prompt]
        return cmd

    def start_session(
        self,
        model: str,
        prompt: str = "",
        session_id: str = "",
        session_name: str = "",
        resume: bool = False,
    ) -> SessionSpec:
        # 同 claude:prompt 烤进 command(--prompt 自动提交),spawner 不注入 PTY。
        # sid 在 spec 里仅作记录(opencode 不认预指定 id,RESUME 时才用捕获到的 sid)。
        return SessionSpec(
            command=self.interactive_launch_cmd(
                model,
                prompt=prompt,
                session_id=session_id,
                session_name=session_name,
                resume=resume,
            ),
            pty_prompt="",  # 已在 command(--prompt)
            readiness_marker=None,  # opencode --prompt 自管理 readiness
            session_id=session_id,
            resume=resume,
        )

    def bypass_flags(self) -> list[str]:
        # --auto 由 interactive_launch_cmd 统一带上(NEW/RESUME 都要自动批准)。
        # 这里返回同样的 flag,供 headless / 仅走 bypass_flags() 的路径对齐。
        return ["--auto"]

    def headless_launch_cmd(self, model: str, prompt: str) -> list[str] | None:
        # `opencode run` —— 无 TUI 的脚本/自动化模式。prompt 走位置参数。
        # 注:prompt 由调用方经 stdin 灌入(见 consult/runner 的 headless 协议);
        # 这里只给启动 argv。模型格式 provider/model。
        cmd = [resolve_executable("opencode"), "run", "--auto"]
        if model:
            cmd += ["--model", model]
        return cmd

    def inject_prompt(self, prompt: str, story_key: str, stage: str) -> str | None:
        # baked-in 路径(prompt 在 command),无需 PTY 注入;仅记 anchor。
        self.write_anchor(prompt, story_key, stage)
        return None

    def cleanup(self, story_key: str, stage: str):
        pass

    # --- sid SQLite 查询捕获 ------------------------------------------------
    def capture_sid_post_exit(
        self,
        story_key: str,
        stage: str,
        cwd: str | None = None,
        since_ts: str | None = None,
    ) -> str | None:
        """clean_exit 后查 opencode.db,取本次 spawn 时间窗内最新 session.id。

        opencode 1.18+ 把会话存进 ``<data>/opencode.db`` 的 ``session`` 表,字段:
        ``id``(ses_…)、``directory``(spawn cwd)、``time_created``(epoch 毫秒)。
        一条 SQL 取代旧版的文件扫描:

            SELECT id FROM session
            WHERE directory = :cwd AND time_created >= :since_ms
            ORDER BY time_created DESC LIMIT 1

        ``since_ts`` 是 spawn 前记的 UTC iso(``_now_utc_iso``),转 epoch 毫秒做下界。
        无 cwd 则放宽(全表靠 since 过滤)。best-effort:db 缺失/查询失败 → None。
        """
        try:
            db_path = _opencode_db_path()
            if not db_path.is_file():
                return None
            # 只读连接(URI mode=ro),绝不干扰 opencode 自己的写入。
            uri = f"file:{db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                sql = "SELECT id FROM session"
                clauses = []
                params: list = []
                if cwd:
                    clauses.append("directory = ?")
                    params.append(str(cwd))
                since_ms = _iso_to_epoch_ms(since_ts) if since_ts else None
                if since_ms is not None:
                    clauses.append("time_created >= ?")
                    params.append(since_ms)
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY time_created DESC LIMIT 1"
                row = conn.execute(sql, params).fetchone()
                return row[0] if row else None
            finally:
                conn.close()
        except Exception as exc:  # best-effort:绝不拖垮 stage 收尾
            log.warning(
                "[%s] opencode sid sqlite-capture failed (%s); resume disabled for stage=%s",
                story_key,
                exc,
                stage,
            )
            return None


def _iso_to_epoch_ms(iso_ts: str) -> int | None:
    """opencode 的 time_created 是 epoch 毫秒;planner 的 since_ts 是 UTC iso(秒精度)。

    把 iso 转成毫秒做 SQL 下界比较。容忍 'Z' / '+00:00' / 无时区(按 UTC)。
    """
    import datetime as _dt

    s = iso_ts.strip()
    if not s:
        return None
    try:
        # 兼容 '...Z' / 带偏移 / 无时区
        s2 = s.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None
