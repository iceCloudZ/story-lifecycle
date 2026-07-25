"""OpenCode adapter (sst/opencode CLI)."""

import json
import logging
import os
from pathlib import Path

from .base import BaseAdapter, SessionSpec
from ...infra.terminal.platform_ops import resolve_executable

log = logging.getLogger(__name__)


def _opencode_data_dir() -> Path:
    """opencode 的 per-user 数据根目录。

    opencode 把会话存成三层 JSON 文件(``<data>/storage/{project,session,message,
    part}/...``)。根目录随平台不同(对应 opencode 的 ``Global.Path.data``):

      - Linux:   ``~/.local/share/opencode``
      - macOS:   ``~/Library/Application Support/opencode``
      - Windows: ``%LOCALAPPDATA%\\opencode``

    未实测确认的部分见 DESIGN-session-pty-id-model.md §2.5(opencode 行 TODO);
    此处给三条平台默认值,可被 env ``OPENCODE_DATA_DIR`` 覆盖。
    """
    override = os.environ.get("OPENCODE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "opencode"
    if sys_darwin():
        return Path.home() / "Library" / "Application Support" / "opencode"
    return Path.home() / ".local" / "share" / "opencode"


def sys_darwin() -> bool:
    import platform

    return platform.system() == "Darwin"


class OpencodeAdapter(BaseAdapter):
    """Adapter for OpenCode CLI (opencode).

    与 claude 同属「prompt baked into command」一族:opencode ``--prompt "..."``
    在 TUI 就绪后自动提交(opencode 源码 home.tsx 证实 args.prompt 有 sent 守卫),
    故 spawner 不做 PTY 注入 / readiness 猜测(``pty_prompt=""``,
    ``readiness_marker=None``)。opencode 的 TUI banner 是 ASCII art,无稳定文本
    可锚定 → 走 baked-in 正好绕开这个坑。

    sid 模型 = 「CLI 自分配、文件扫描捕获」:opencode 不支持预指定 session id,
    终端也不吐 sid;会话写进 ``<data>/storage/session/<projectID>/<sid>.json``。
    capture_sid_post_exit 在 clean_exit 后扫该目录,按 ``time.created >= since_ts``
    取最新 session.id 回填。详见 AGENTS.md「Session-id model」与 DESIGN §2.5。
    """

    name = "opencode"

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

    # --- sid 文件扫描捕获 ---------------------------------------------------
    def capture_sid_post_exit(
        self,
        story_key: str,
        stage: str,
        cwd: str | None = None,
        since_ts: str | None = None,
    ) -> str | None:
        """clean_exit 后扫 opencode 存储目录,取本次 spawn 时间窗内最新 session.id。

        步骤:
          1. 反查 projectID:扫 ``<data>/storage/project/*.json``,取 ``directory``
             规范化后等于 cwd 的 project(无 cwd 则取所有)。
          2. 列 ``<data>/storage/session/<projectID>/*.json``,读 ``time.created``。
          3. 过滤 ``created >= since_ts``(spawn 前的 UTC iso),取最新者。
          4. 返回 ``info.id``(``ses_…``)。

        best-effort:任何一步缺目录/解析失败 → 返回 None(下次当新会话,不崩)。
        """
        try:
            storage = _opencode_data_dir() / "storage"
            session_root = storage / "session"
            if not session_root.is_dir():
                return None

            project_dirs = self._match_project_dirs(storage, cwd)
            if not project_dirs:
                # cwd 没匹配到 project(或没给 cwd)→ 扫全部,靠 since_ts 过滤。
                project_dirs = [d for d in session_root.iterdir() if d.is_dir()]
            if not project_dirs:
                return None

            best_sid = None
            best_ts = None
            for pdir in project_dirs:
                for sf in pdir.glob("*.json"):
                    created = self._session_created(sf)
                    if created is None:
                        continue
                    if since_ts and created < since_ts:
                        continue
                    if best_ts is None or created > best_ts:
                        best_ts = created
                        best_sid = self._session_id(sf)
            return best_sid
        except Exception as exc:  # best-effort:绝不拖垮 stage 收尾
            log.warning(
                "[%s] opencode sid file-scan failed (%s); resume disabled for stage=%s",
                story_key,
                exc,
                stage,
            )
            return None

    @staticmethod
    def _match_project_dirs(storage: Path, cwd: str | None) -> list[Path]:
        session_root = storage / "session"
        if not cwd:
            return []
        cwd_norm = os.path.normpath(str(cwd))
        matched: list[Path] = []
        for pf in (storage / "project").glob("*.json"):
            try:
                info = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if os.path.normpath(str(info.get("directory", ""))) == cwd_norm:
                pid = info.get("id") or pf.stem
                pdir = session_root / str(pid)
                if pdir.is_dir():
                    matched.append(pdir)
        return matched

    @staticmethod
    def _session_created(session_file: Path) -> str | None:
        try:
            info = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        ts = (info.get("time") or {}).get("created")
        return ts if isinstance(ts, str) else None

    @staticmethod
    def _session_id(session_file: Path) -> str | None:
        try:
            info = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        sid = info.get("id")
        return sid if isinstance(sid, str) else session_file.stem
