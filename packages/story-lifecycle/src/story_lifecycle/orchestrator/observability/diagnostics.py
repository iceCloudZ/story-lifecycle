"""Diagnostic bundle generation — collect, redact, package.

Story-level bundles go to {workspace}/.story/diagnostics/{key}-{timestamp}.zip
Global bundles go to ~/.story-lifecycle/diagnostics/global-{timestamp}.zip
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .debug_packet import build_debug_packet, redact_text, redact_mapping
from ...infra.db import models as db


def create_story_diagnostics_bundle(
    story_key: str,
    output_path: str | None = None,
    include_diff: bool = False,
    event_limit: int = 200,
    no_zip: bool = False,
) -> dict:
    """Generate a diagnostic bundle for a story.

    Returns {"path": str} on success, {"error": str} on failure.
    """
    packet = build_debug_packet(story_key)
    if "error" in packet:
        return packet

    workspace = packet["story"]["workspace"]
    ws_path = Path(workspace)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if output_path:
        out_dir = Path(output_path)
    elif no_zip:
        out_dir = ws_path / ".story" / "diagnostics" / f"{story_key}-{ts}"
    else:
        out_dir = ws_path / ".story" / "diagnostics"

    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "bundle_type": "story",
        "story_key": story_key,
        "created_at": datetime.now().isoformat(),
        "story_lifecycle_version": _get_version(),
        "workspace": workspace,
        "files": [],
        "missing": [],
        "truncated": [],
    }

    bundle_dir = (
        out_dir if no_zip else Path(tempfile.mkdtemp(prefix=f"diag-{story_key}-"))
    )

    # 1. debug_packet.json
    _write_json(bundle_dir / "debug_packet.json", packet)
    manifest["files"].append(
        {"path": "debug_packet.json", "kind": "json", "redacted": False}
    )

    # 2. story.json (DB row, redacted)
    story_data = db.get_story(story_key) or {}
    _write_json(bundle_dir / "story.json", redact_mapping(story_data))
    manifest["files"].append({"path": "story.json", "kind": "json", "redacted": True})

    # 3. events.jsonl
    events = db.get_story_events(story_key)
    _write_jsonl(bundle_dir / "events.jsonl", events[-event_limit:])
    manifest["files"].append(
        {"path": "events.jsonl", "kind": "jsonl", "redacted": False}
    )
    if len(events) > event_limit:
        manifest["truncated"].append(
            {
                "path": "events.jsonl",
                "reason": f"limited to {event_limit} of {len(events)}",
            }
        )

    # 4. stage_logs.jsonl
    stage_logs = db.get_stage_logs(story_key, limit=100)
    _write_jsonl(bundle_dir / "stage_logs.jsonl", stage_logs)
    manifest["files"].append(
        {"path": "stage_logs.jsonl", "kind": "jsonl", "redacted": False}
    )

    # 5. gate_results.jsonl
    gate_results = db.get_gate_results(story_key, limit=50)
    _write_jsonl(bundle_dir / "gate_results.jsonl", gate_results)
    manifest["files"].append(
        {"path": "gate_results.jsonl", "kind": "jsonl", "redacted": False}
    )

    # 6. config.redacted.yaml
    _collect_redacted_config(bundle_dir, manifest)

    # 7. environment.txt
    _collect_environment(bundle_dir, manifest)

    # 8. done/ files
    _collect_done_files(bundle_dir, ws_path, story_key, packet, manifest)

    # 9. context/ files
    _collect_context_files(bundle_dir, ws_path, story_key, manifest)

    # 10. terminal/
    _collect_terminal_output(bundle_dir, story_key, packet, manifest)

    # 11. workspace/ (git status, optional git diff)
    if ws_path.exists():
        _collect_git_info(bundle_dir, ws_path, include_diff, manifest)
    else:
        manifest["missing"].append(
            {"path": "workspace/", "reason": "workspace directory does not exist"}
        )

    # 12. summary.md
    _write_summary_md(bundle_dir, packet, manifest)

    # 13. manifest.json
    _write_json(bundle_dir / "manifest.json", manifest)

    # Zip or return directory path
    if no_zip:
        return {"path": str(bundle_dir)}

    zip_path = out_dir / f"{story_key}-{ts}.zip"
    _make_zip(bundle_dir, zip_path)
    return {"path": str(zip_path)}


def create_global_diagnostics_bundle(
    output_path: str | None = None,
    no_zip: bool = False,
) -> dict:
    """Generate a system-wide diagnostic bundle (no specific story).

    Returns {"path": str} on success, {"error": str} on failure.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    home = Path.home() / ".story-lifecycle"

    if output_path:
        out_dir = Path(output_path)
    else:
        out_dir = home / "diagnostics"

    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "bundle_type": "global",
        "story_key": "",
        "created_at": datetime.now().isoformat(),
        "story_lifecycle_version": _get_version(),
        "workspace": "",
        "files": [],
        "missing": [],
        "truncated": [],
    }

    bundle_dir = out_dir if no_zip else Path(tempfile.mkdtemp(prefix="diag-global-"))

    # 1. environment.txt
    _collect_environment(bundle_dir, manifest)

    # 2. config.redacted.yaml
    _collect_redacted_config(bundle_dir, manifest)

    # 3. commands/ help output
    cmds_dir = bundle_dir / "commands"
    cmds_dir.mkdir(parents=True, exist_ok=True)
    for cmd_name, cmd_args in [
        ("story_help.txt", [os.sys.executable, "-m", "story_lifecycle", "--help"]),
        (
            "story_setup_help.txt",
            [os.sys.executable, "-m", "story_lifecycle", "setup", "--help"],
        ),
        (
            "story_doctor_help.txt",
            [os.sys.executable, "-m", "story_lifecycle", "doctor", "--help"],
        ),
    ]:
        try:
            r = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            (cmds_dir / cmd_name).write_text(
                r.stdout or r.stderr or "(empty)", encoding="utf-8"
            )
            manifest["files"].append(
                {"path": f"commands/{cmd_name}", "kind": "text", "redacted": False}
            )
        except Exception as exc:
            manifest["missing"].append(
                {"path": f"commands/{cmd_name}", "reason": str(exc)}
            )

    # 4. package/ metadata
    pkg_dir = bundle_dir / "package"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    try:
        from importlib.metadata import metadata

        meta = metadata("story-lifecycle")
        meta_dict = {str(k): str(v) for k, v in meta.items()}
        _write_json(pkg_dir / "metadata.json", meta_dict)
        manifest["files"].append(
            {"path": "package/metadata.json", "kind": "json", "redacted": False}
        )
    except Exception as exc:
        manifest["missing"].append(
            {"path": "package/metadata.json", "reason": str(exc)}
        )

    # 5. logs/ tail
    logs_dir = bundle_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for log_name in ["graph_error.log", "planner_error.log"]:
        log_path = home / log_name
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            if len(content) > 200 * 1024:
                content = content[-200 * 1024 :]
                manifest["truncated"].append(
                    {"path": f"logs/{log_name}_tail.log", "size_limit": "200KB"}
                )
            (logs_dir / f"{log_name}_tail.log").write_text(content, encoding="utf-8")
            manifest["files"].append(
                {"path": f"logs/{log_name}_tail.log", "kind": "text", "redacted": True}
            )
        else:
            manifest["missing"].append(
                {"path": f"logs/{log_name}_tail.log", "reason": "file does not exist"}
            )

    # 6. summary.md
    summary_lines = [
        "# 全局诊断报告",
        "",
        f"- **生成时间**: {datetime.now().isoformat()}",
        f"- **版本**: {_get_version()}",
        f"- **平台**: {os.name}",
        "",
        "## 包内容状态",
        "",
    ]
    for f in manifest.get("files", []):
        summary_lines.append(f"- {f['path']}: present")
    for m in manifest.get("missing", []):
        summary_lines.append(f"- {m['path']}: **missing** — {m['reason']}")
    (bundle_dir / "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")
    manifest["files"].append(
        {"path": "summary.md", "kind": "markdown", "redacted": False}
    )

    # 7. manifest.json
    _write_json(bundle_dir / "manifest.json", manifest)

    # Zip or return directory
    if no_zip:
        return {"path": str(bundle_dir)}

    zip_path = out_dir / f"global-{ts}.zip"
    _make_zip(bundle_dir, zip_path)
    return {"path": str(zip_path)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("story-lifecycle")
    except Exception:
        return "unknown"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _make_zip(src_dir: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(dest), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src_dir))
    shutil.rmtree(src_dir, ignore_errors=True)


def _collect_redacted_config(bundle_dir: Path, manifest: dict) -> None:
    config_path = Path.home() / ".story-lifecycle" / "config.yaml"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        dest = bundle_dir / "config.redacted.yaml"
        dest.write_text(redact_text(text), encoding="utf-8")
        manifest["files"].append(
            {"path": "config.redacted.yaml", "kind": "yaml", "redacted": True}
        )
    else:
        manifest["missing"].append(
            {"path": "config.redacted.yaml", "reason": "no config file"}
        )


def _collect_environment(bundle_dir: Path, manifest: dict) -> None:
    lines = [
        f"platform: {os.name}",
        f"python: {os.sys.version}",
        f"executable: {os.sys.executable}",
        f"cwd: {os.getcwd()}",
        f"path: {os.environ.get('PATH', '')}",
    ]
    dest = bundle_dir / "environment.txt"
    dest.write_text("\n".join(lines), encoding="utf-8")
    manifest["files"].append(
        {"path": "environment.txt", "kind": "text", "redacted": False}
    )


def _collect_done_files(
    bundle_dir: Path, ws_path: Path, story_key: str, packet: dict, manifest: dict
) -> None:
    from ...infra.paths import stage_done_file, malformed_done_file, context_dir

    stage = packet["story"]["current_stage"]

    # current done
    done_p = stage_done_file(ws_path, story_key, stage)
    done_dest = bundle_dir / "done" / "current.json"
    if done_p.exists():
        done_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(done_p, done_dest)
        manifest["files"].append(
            {"path": "done/current.json", "kind": "json", "redacted": False}
        )
    else:
        manifest["missing"].append(
            {"path": "done/current.json", "reason": "no done file"}
        )

    # malformed
    mal_p = malformed_done_file(ws_path, story_key, stage)
    if mal_p.exists():
        mal_dest = bundle_dir / "done" / "current.malformed"
        mal_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mal_p, mal_dest)
        manifest["files"].append(
            {"path": "done/current.malformed", "kind": "text", "redacted": False}
        )

    # snapshots
    snapshot_dir = context_dir(ws_path, story_key) / "done"
    if snapshot_dir.exists():
        snap_dest = bundle_dir / "done" / "snapshots"
        snap_dest.mkdir(parents=True, exist_ok=True)
        for f in snapshot_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, snap_dest / f.name)
        manifest["files"].append(
            {"path": "done/snapshots/", "kind": "dir", "redacted": False}
        )


def _collect_context_files(
    bundle_dir: Path, ws_path: Path, story_key: str, manifest: dict
) -> None:
    from ...infra.paths import context_dir

    ctx_dir = context_dir(ws_path, story_key)
    if ctx_dir.exists():
        files_list = []
        for f in sorted(ctx_dir.rglob("*")):
            if f.is_file():
                files_list.append(str(f.relative_to(ctx_dir)))
        dest = bundle_dir / "context" / "known_context_files.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            "\n".join(files_list) if files_list else "(empty)", encoding="utf-8"
        )
        manifest["files"].append(
            {
                "path": "context/known_context_files.txt",
                "kind": "text",
                "redacted": False,
            }
        )
    else:
        manifest["missing"].append(
            {"path": "context/", "reason": "context directory does not exist"}
        )


def _collect_terminal_output(
    bundle_dir: Path, story_key: str, packet: dict, manifest: dict
) -> None:
    session_name = packet["session_state"].get("session_name", "")
    if not session_name:
        manifest["missing"].append(
            {"path": "terminal/recent_output.txt", "reason": "no session name"}
        )
        return

    dest = bundle_dir / "terminal" / "recent_output.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["zellij", "action", "dump-screen", session_name, "-n", "500"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.splitlines()
            dest.write_text(result.stdout, encoding="utf-8")
            manifest["files"].append(
                {
                    "path": "terminal/recent_output.txt",
                    "kind": "text",
                    "redacted": False,
                }
            )
            if len(lines) >= 500:
                manifest["truncated"].append(
                    {"path": "terminal/recent_output.txt", "line_limit": 500}
                )
        else:
            manifest["missing"].append(
                {
                    "path": "terminal/recent_output.txt",
                    "reason": result.stderr.strip() or "empty output",
                }
            )
    except FileNotFoundError:
        manifest["missing"].append(
            {"path": "terminal/recent_output.txt", "reason": "zellij not available"}
        )
    except subprocess.TimeoutExpired:
        manifest["missing"].append(
            {"path": "terminal/recent_output.txt", "reason": "zellij dump timed out"}
        )
    except Exception as exc:
        manifest["missing"].append(
            {"path": "terminal/recent_output.txt", "reason": str(exc)}
        )

    # session state
    _write_json(bundle_dir / "terminal" / "session_state.json", packet["session_state"])
    manifest["files"].append(
        {"path": "terminal/session_state.json", "kind": "json", "redacted": False}
    )


def _collect_git_info(
    bundle_dir: Path, ws_path: Path, include_diff: bool, manifest: dict
) -> None:
    dest_dir = bundle_dir / "workspace"
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        r = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            cwd=str(ws_path),
            timeout=15,
        )
        if r.returncode == 0:
            (dest_dir / "git_status.txt").write_text(
                r.stdout or "(clean)", encoding="utf-8"
            )
            manifest["files"].append(
                {"path": "workspace/git_status.txt", "kind": "text", "redacted": False}
            )
        else:
            manifest["missing"].append(
                {"path": "workspace/git_status.txt", "reason": r.stderr.strip()}
            )
    except Exception as exc:
        manifest["missing"].append(
            {"path": "workspace/git_status.txt", "reason": str(exc)}
        )

    if include_diff:
        try:
            r = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True,
                text=True,
                cwd=str(ws_path),
                timeout=15,
            )
            if r.returncode == 0:
                content = r.stdout or "(no diff)"
                if len(content) > 200 * 1024:
                    content = content[: 200 * 1024] + "\n... [truncated at 200KB]"
                    manifest["truncated"].append(
                        {"path": "workspace/git_diff_stat.txt", "size_limit": "200KB"}
                    )
                (dest_dir / "git_diff_stat.txt").write_text(content, encoding="utf-8")
                manifest["files"].append(
                    {
                        "path": "workspace/git_diff_stat.txt",
                        "kind": "text",
                        "redacted": False,
                    }
                )
        except Exception as exc:
            manifest["missing"].append(
                {"path": "workspace/git_diff_stat.txt", "reason": str(exc)}
            )


def _write_summary_md(bundle_dir: Path, packet: dict, manifest: dict) -> None:
    """Generate human-readable summary.md."""
    s = packet["story"]
    stuck = packet["stuck_reason"]
    events = packet.get("recent_events", [])

    lines = [
        f"# 诊断报告: {s['story_key']}",
        "",
        f"- **状态**: {s['status']} / {s['current_stage']}",
        f"- **Workspace**: {s['workspace']}",
        f"- **卡住原因**: {stuck['code']}",
        f"- **说明**: {stuck['message']}",
        "",
        "## 最近关键事件",
        "",
    ]

    for ev in events[-10:]:
        et = ev.get("event_type", "?")
        ts = ev.get("created_at", "?")
        lines.append(f"- {ts} {et}")

    lines.extend(
        [
            "",
            "## 重点关注",
            "",
        ]
    )

    code = stuck["code"]
    if code == "cli_exited_without_done":
        lines.append(
            "1. 先看 `terminal/recent_output.txt`，确认 Agent 执行命令是否报错。"
        )
        lines.append("2. 再看 `debug_packet.json` 的 `done_state`。")
        lines.append("3. 如果存在 malformed done，查看 `done/current.malformed`。")
    elif code == "stage_timeout":
        lines.append("1. 检查 `terminal/recent_output.txt` 是否停在长耗时命令。")
        lines.append("2. 可能是依赖安装、测试命令或大文件操作。")
    elif code == "loop_exhausted":
        lines.append(
            "1. 查看 `events.jsonl` 中的 evaluator_loop_round 和 evaluator_loop_completed 事件。"
        )
        lines.append("2. 关注 `no_progress` 和 `decision: fail` 标记。")
    elif code == "done_malformed":
        lines.append("1. 查看 `done/current.malformed` 了解损坏的 JSON 内容。")
        lines.append("2. 手动修复或删除 `.story/done/{key}/{stage}.json`。")
    elif code == "gate_blocked":
        lines.append("1. 查看 `events.jsonl` 中的 gate_decision 事件。")
        lines.append("2. 按 `A` 接受风险推进，或按 `r` 重试 review。")
    else:
        lines.append("1. 查看 `debug_packet.json` 了解完整诊断数据。")
        lines.append("2. 查看 `events.jsonl` 了解事件时间线。")

    lines.extend(
        [
            "",
            "## 包内容状态",
            "",
        ]
    )

    for f in manifest.get("files", []):
        path = f["path"]
        redacted = " (redacted)" if f.get("redacted") else ""
        lines.append(f"- {path}: present{redacted}")

    for m in manifest.get("missing", []):
        lines.append(f"- {m['path']}: **missing** — {m['reason']}")

    for t in manifest.get("truncated", []):
        reason = t.get("line_limit", t.get("size_limit", "unknown"))
        lines.append(f"- {t['path']}: truncated ({reason})")

    dest = bundle_dir / "summary.md"
    dest.write_text("\n".join(lines), encoding="utf-8")
    manifest["files"].append(
        {"path": "summary.md", "kind": "markdown", "redacted": False}
    )
