"""结果轴 phase 2 Brief F：按 12 类 task_type 挖过程知识。

输入：
  - transcripts.db（hc-all 会话）
  - scripts/out/story_task_types.json（story → task_type）
输出：
  - <workspace>/.story/knowledge/playbooks/{task_type}.md
  - <workspace>/.story/knowledge/failures/{task_type}.md
  - <workspace>/.story/knowledge/playbooks/INDEX.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJ))
from miner import config  # noqa: E402
from miner.common import ws_of  # noqa: E402

DB = config.DB_PATH

# 12 类 task_type 受控词表 + 关键词（用于 unbound session 分类）
TASK_TYPE_KEYWORDS = {
    "credit-limit": ["授信", "额度", "风控", "增信", "提额", "credit", "limit", "risk", "授信节点"],
    "fund-flow": ["放款", "还款", "提现", "清分", "对账", "借贷", "repay", "withdraw", "loan", "fund"],
    "message-notify": ["短信", "OTP", "通知", "模板", "whatsapp", "sms", "message", "notify", "路由"],
    "marketing": ["营销", "活动", "MGM", "券", "免息", "奖励", "coupon", "activity", "marketing"],
    "user-profile": ["用户", "资料", "认证", "隐私", "KYC", "user", "profile", "联系人"],
    "order": ["订单", "交易", "order", "borrow", "liquidate"],
    "integration": ["三方", "对接", "回调", "third-party", "callback", "integration"],
    "gateway-infra": ["网关", "限流", "配置", "调度", "状态机", "gateway", "config", "infra"],
    "data-sql": ["SQL", "查询", "迁移", "schema", "sql", "data", "DDL", "表结构"],
    "frontend": ["前端", "admin", "页面", "frontend", "protable", "proform", "组件"],
    "deploy": ["部署", "上线", "发版", "deploy", "release", "skyladder", "nexus"],
    "debug": ["排查", "定位", "debug", "为什么", "报错", "日志", "异常"],
}

CODE_EXT = (".java", ".ts", ".tsx", ".sql", ".xml", ".yml", ".yaml")

ROLE_RULES = [
    ("Controller", [r"/controller/", r"Controller\.java"]),
    ("ServiceImpl", [r"/service/impl/", r"ServiceImpl\.java"]),
    ("Service(接口)", [r"/service/[^/]+\.java"]),
    ("Mapper/DAO", [r"/mapper/", r"/dao/", r"Mapper\.java", r"Mapper\.xml"]),
    ("Entity/VO/DTO", [r"/entity/", r"/vo/", r"/dto/", r"/domain/", r"Entity\.java", r"DTO\.java", r"VO\.java"]),
    ("Processor", [r"Processor\.java", r"/processor/"]),
    ("Liquidate(清分)", [r"/liquidate/", r"Liquidate"]),
    ("Validator", [r"/validator/", r"Validator\.java"]),
    ("Component", [r"/component/", r"Component\.java"]),
    ("Listener/MQ", [r"/listener/", r"/mq/", r"/consumer/", r"Listener\.java"]),
    ("Config", [r"/config/", r"Config\.java", r"\.yml", r"\.yaml"]),
    ("Job(定时)", [r"/job/", r"Job\.java", r"XxlJob"]),
    ("Enum", [r"/enums?/", r"Enum\.java"]),
    ("Util", [r"/util/", r"Util\.java", r"Utils\.java"]),
]

_HC_SERVICES = tuple(
    config._cfg.get(
        "service_names",
        (
            "hc-order",
            "hc-user",
            "hc-limit",
            "hc-message",
            "hc-third-party",
            "hc-config",
            "hc-coupon",
            "hc-marketing",
            "hc-gateway",
            "hc-callback",
            "hc-job",
        ),
    )
)


def infer_role(p: str) -> str | None:
    for role, patterns in ROLE_RULES:
        for pat in patterns:
            if re.search(pat, p, re.IGNORECASE):
                return role
    return None


def short(p: str, max_len: int = 70) -> str:
    p = p.replace("\\", "/")
    parts = [x for x in p.split("/") if x]
    basename = parts[-1] if parts else p
    svc = None
    for x in parts:
        if x in _HC_SERVICES:
            svc = x
            break
    java_idx = None
    for i, x in enumerate(parts):
        if x in ("java", "resources"):
            java_idx = i
            break
    if java_idx is not None:
        tail = parts[java_idx + 1 :]
        if len(tail) >= 4 and tail[0] == "com" and tail[1] == "ys" and tail[2] == "hc":
            shown = "/".join(tail[3:])
        else:
            shown = "/".join(tail)
        if svc and not shown.startswith(svc + "/"):
            shown = f"{svc}/{shown}"
        return _truncate(shown, max_len)
    parent = parts[-2] if len(parts) >= 2 else ""
    shown = f"{parent}/{basename}" if parent else basename
    if svc and not shown.startswith(svc):
        shown = f"{svc}/{shown}"
    return _truncate(shown, max_len)


def _truncate(shown: str, max_len: int) -> str:
    if len(shown) <= max_len:
        return shown
    segs = shown.split("/")
    basename = segs[-1]
    parent = segs[-2] if len(segs) >= 2 else ""
    head_segs = segs[:-2]
    tail = f"{parent}/{basename}" if parent else basename
    head_budget = max_len - len(tail) - 2
    if head_budget <= 6:
        return "…/" + tail
    kept = []
    used = 0
    for seg in head_segs:
        add = len(seg) + (1 if kept else 0)
        if used + add > head_budget:
            break
        kept.append(seg)
        used += add
    head = "/".join(kept)
    return f"{head}/…/{tail}" if head else f"…/{tail}"


def cmd_class(cmd: str) -> str | None:
    c = (cmd or "").strip()
    if "cli_skyladder" in c:
        return "cli_skyladder(部署)"
    if "cli_sql" in c:
        return "cli_sql(查库)"
    if "cli_behavior" in c or "cli_es" in c:
        return "cli_behavior/es(日志)"
    if c.startswith("curl"):
        return "curl(调API)"
    if c.startswith("ssh"):
        return "ssh(登机)"
    if c.startswith("git"):
        return "git"
    if c.startswith(("find", "grep", "rg")):
        return "search(find/grep)"
    if c.startswith(("python", "python3")):
        return "python(脚本)"
    return None


def classify_failure(text: str) -> str | None:
    t = (text or "").lower()
    if "exit code: 0" in t:
        return None
    if "cannot find symbol" in t or "compile" in t or "build fail" in t:
        return "编译/构建错误"
    if "conflict" in t or "merge" in t:
        return "Git冲突/状态"
    if "no such file" in t or "filenotfound" in t or "does not exist" in t:
        return "文件/路径不存在"
    if "timeout" in t or "timed out" in t:
        return "超时/被kill"
    if "nullpointer" in t or "classcast" in t or "illegalarg" in t:
        return "类型错误"
    if "permission" in t or "denied" in t:
        return "权限拒绝"
    return None


def load_story_task_types(path: Path, workspace_id: str) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    prefix = "11" + workspace_id + "00"
    mapping: dict[str, str] = {}
    for item in data:
        full_id = item["story_key"].replace("tapd-", "")
        short_id = full_id.removeprefix(prefix).lstrip("0")
        mapping[short_id] = item["task_type"]
    return mapping


def classify_session(text: str) -> str | None:
    """用关键词把 unbound session 分到 12 类 task_type。"""
    t = (text or "").lower()
    scores: dict[str, int] = defaultdict(int)
    for task_type, kws in TASK_TYPE_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in t:
                scores[task_type] += 1
    if not scores:
        return None
    return max(scores.items(), key=lambda x: (x[1], x[0]))[0]


def aggregate(c: sqlite3.Cursor, sids: list[str]):
    files = Counter()
    file_roles: dict[str, str] = {}
    file_display: dict[str, str] = {}

    ph = ",".join("?" * len(sids))
    c.execute(
        f"SELECT path FROM events WHERE kind='tool' AND name IN ('Read','Grep','Glob') "
        f"AND COALESCE(path,'')<>'' AND sid IN ({ph})",
        sids,
    )
    for (p,) in c.fetchall():
        disp = p.replace("\\", "/")
        if not disp.endswith(CODE_EXT):
            continue
        key = disp.lower()
        files[key] += 1
        if key not in file_display:
            file_display[key] = disp
        if key not in file_roles:
            file_roles[key] = infer_role(key) or "—"

    cmds = Counter()
    c.execute(
        f"SELECT cmd FROM events WHERE kind='tool' AND name='Bash' "
        f"AND COALESCE(cmd,'')<>'' AND sid IN ({ph})",
        sids,
    )
    for (cmd,) in c.fetchall():
        cls = cmd_class(cmd)
        if cls:
            cmds[cls] += 1

    fails = Counter()
    fail_examples: dict[str, list[str]] = defaultdict(list)
    c.execute(
        f"SELECT text FROM events WHERE kind='result' AND ok=0 "
        f"AND COALESCE(text,'')<>'' AND sid IN ({ph})",
        sids,
    )
    for (t,) in c.fetchall():
        fc = classify_failure(t)
        if fc:
            fails[fc] += 1
            if len(fail_examples[fc]) < 3:
                fail_examples[fc].append(t[:200].replace("\n", " "))

    return files, file_display, file_roles, cmds, fails, fail_examples


def render_files_table(files: Counter, file_display: dict, file_roles: dict, top: int = 15):
    out = ["| 文件 | 角色 | 次数 |", "|---|---|---|"]
    for key, n in files.most_common(top):
        role = file_roles.get(key) or "—"
        disp = file_display.get(key, key)
        out.append(f"| `{short(disp)}` | {role} | {n} |")
    return out


def write_playbook(out_dir: Path, task_type: str, sids: list[str], c: sqlite3.Cursor):
    files, file_display, file_roles, cmds, fails, fail_examples = aggregate(c, sids)
    label = TASK_TYPE_KEYWORDS[task_type][0]

    lines = [f"# {label} Playbook", ""]
    lines.append(
        f"> 任务类型：`{task_type}` | 历史会话数：**{len(sids)}** | "
        f"来源：transcripts.db 中按 story_id 或关键词归类的会话。"
    )
    lines.append(
        "> 文件为历史高频访问，**代码可能已变，使用前用 codegraph 核验当前状态**。"
    )
    lines.append("")

    if files:
        lines.append("## 高频访问文件 Top")
        lines.extend(render_files_table(files, file_display, file_roles))
        lines.append("")

    if cmds:
        lines.append("## 常用操作")
        for cls, n in cmds.most_common(8):
            lines.append(f"- {cls}: {n}")
        lines.append("")

    if fails:
        lines.append("## 常见失败")
        for fc, n in fails.most_common(6):
            lines.append(f"- **{fc}**: {n} 次")
            for ex in fail_examples.get(fc, []):
                lines.append(f"  - 例：{ex}")
        lines.append("")

    out_path = out_dir / f"{task_type}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_failure(out_dir: Path, task_type: str, sids: list[str], c: sqlite3.Cursor):
    _, _, _, _, fails, fail_examples = aggregate(c, sids)
    label = TASK_TYPE_KEYWORDS[task_type][0]

    lines = [f"# {label} 失败模式", ""]
    lines.append(f"> 任务类型：`{task_type}` | 样本会话数：**{len(sids)}** | 来源：transcripts.db")
    lines.append("")

    if not fails:
        lines.append("_该任务类型下未挖掘到明确失败模式（样本不足或失败较少）。_")
        lines.append("")
    else:
        lines.append("| 失败类型 | 次数 | 占比 |")
        lines.append("|---|---|---|")
        total = sum(fails.values())
        for fc, n in fails.most_common():
            lines.append(f"| {fc} | {n} | {n/total*100:.1f}% |")
        lines.append("")
        lines.append("## 典型失败示例")
        for fc, n in fails.most_common(6):
            lines.append(f"### {fc}（{n} 次）")
            for ex in fail_examples.get(fc, []):
                lines.append(f"- {ex}")
            lines.append("")

    out_path = out_dir / f"{task_type}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_index(out_dir: Path, task_counts: dict[str, int], task_files: dict[str, int]):
    lines = ["# 任务类型 Playbooks Index", ""]
    lines.append(
        "> 按 12 类 task_type 整理的过程知识（playbook + failure）。"
        "稀疏的类已标为盲区，呼应发散点 5。"
    )
    lines.append("")
    lines.append("## Playbooks")
    lines.append("")
    lines.append("| 任务类型 | 会话数 | 高频文件数 | 状态 |")
    lines.append("|---|---|---|---|")
    for tt in TASK_TYPE_KEYWORDS:
        n_sessions = task_counts.get(tt, 0)
        n_files = task_files.get(tt, 0)
        status = "✅ 有 playbook" if n_sessions >= 3 else "🟡 盲区（样本 <3）"
        lines.append(f"| [{tt}](playbooks/{tt}.md) | {n_sessions} | {n_files} | {status} |")
    lines.append("")
    lines.append("## Failures")
    lines.append("")
    for tt in TASK_TYPE_KEYWORDS:
        n_sessions = task_counts.get(tt, 0)
        status = "✅ 有 failure" if n_sessions >= 3 else "🟡 盲区"
        lines.append(f"- [{tt}](failures/{tt}.md) — {n_sessions} 会话 — {status}")
    lines.append("")

    blind_spots = [tt for tt in TASK_TYPE_KEYWORDS if task_counts.get(tt, 0) < 3]
    if blind_spots:
        lines.append("## 盲区（样本不足 <3）")
        lines.append(", ".join(f"`{tt}`" for tt in blind_spots))
        lines.append("")

    (out_dir / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workspace",
        "-w",
        default="D:/hc-all",
        help="目标 workspace 路径（默认 D:/hc-all）",
    )
    ap.add_argument(
        "--task-types",
        default=str(_PROJ / "scripts" / "out" / "story_task_types.json"),
    )
    ap.add_argument("--workspace-id", default="44381896")
    args = ap.parse_args()

    workspace = Path(args.workspace)
    ws_tag = ws_of(str(workspace))
    story_type = load_story_task_types(Path(args.task_types), args.workspace_id)
    print(f"loaded {len(story_type)} story→task_type mappings")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    sessions = list(
        c.execute(
            "SELECT sid, title, first_ucmd, story_id FROM sessions WHERE ws=?",
            (ws_tag,),
        )
    )
    print(f"[{ws_tag}] total sessions: {len(sessions)}")

    sids_by_type: dict[str, list[str]] = defaultdict(list)
    unbound_classified = 0
    unbound_unclear = 0

    for sid, title, fu, story_id in sessions:
        task_type = None
        if story_id and story_id in story_type:
            task_type = story_type[story_id]
        else:
            # unbound session：用 first_ucmd + title 关键词分类
            text = f"{fu or ''} {title or ''}"
            task_type = classify_session(text)
            if task_type:
                unbound_classified += 1
            else:
                unbound_unclear += 1
        if task_type:
            sids_by_type[task_type].append(sid)

    print(f"  bound→task_type: {sum(1 for sid,_,_,sid2 in sessions if sid2 and sid2 in story_type)}")
    print(f"  unbound classified: {unbound_classified}")
    print(f"  unbound unclear: {unbound_unclear}")

    playbooks_dir = workspace / ".story" / "knowledge" / "playbooks"
    failures_dir = workspace / ".story" / "knowledge" / "failures"
    playbooks_dir.mkdir(parents=True, exist_ok=True)
    failures_dir.mkdir(parents=True, exist_ok=True)

    task_counts: dict[str, int] = {}
    task_files: dict[str, int] = {}

    for task_type in TASK_TYPE_KEYWORDS:
        sids = sids_by_type.get(task_type, [])
        task_counts[task_type] = len(sids)
        if len(sids) < 3:
            print(f"[{task_type}] skipped (only {len(sids)} sessions)")
            continue
        pb_path = write_playbook(playbooks_dir, task_type, sids, c)
        fl_path = write_failure(failures_dir, task_type, sids, c)
        # 统计文件数
        files, _, _, _, _, _ = aggregate(c, sids)
        task_files[task_type] = len(files)
        print(f"[{task_type}] wrote {pb_path.name} + {fl_path.name} ({len(sids)} sessions, {len(files)} files)")

    write_index(workspace / ".story" / "knowledge", task_counts, task_files)
    print(f"wrote INDEX.md to {workspace / '.story' / 'knowledge'}")

    summary = {
        "workspace": str(workspace),
        "ws_tag": ws_tag,
        "total_sessions": len(sessions),
        "task_counts": dict(task_counts),
        "task_files": task_files,
        "blind_spots": [tt for tt in TASK_TYPE_KEYWORDS if task_counts.get(tt, 0) < 3],
    }
    out_json = _PROJ / "scripts" / "out" / "task_type_knowledge.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote summary to {out_json}")


if __name__ == "__main__":
    main()
