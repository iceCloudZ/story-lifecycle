"""⑩a: 从 transcript 挖"任务类型 → 必看文件/常用命令/常见失败"，反哺 <workspace>/.story/knowledge/playbooks/。

按 first_ucmd 主题给 session 打标，聚合该类 session 的 events：
  - playbooks/<theme>.md      按任务类型的 playbook（文件名稳定，skill 引用）
  - playbooks/by-story/<id>.md 按 story 聚合的 playbook（仅对关联了 story_id 的 session）

v3 (M6): 工作区与输出路径改为 config 驱动，不再硬编码 hc-all。
"""
import sqlite3, collections, re, os, json, sys, argparse

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)
from miner import config  # noqa: E402
from miner.common import ws_of  # noqa: E402

DB = config.DB_PATH

# 默认主题分类；可在 config.json 中以 "playbook_themes" 覆盖
default_themes = {
    'requirement-dev': ('需求开发', ['实现', '编码', 'feature', 'spec', 'story', 'tapd', '设计文档', '开发', '职业', '字段']),
    'debug': ('排查/Debug', ['排查', 'debug', '为什么', '报错', 'bug', '日志', '没收到', '没进', '失败', '异常']),
    'sms-marketing': ('短信/营销', ['短信', 'sms', '免息', 'mgm', '营销', '活动', '奖励']),
    'deploy': ('部署/上线', ['deploy', '部署', '上线', 'skyladder', '发版', 'nexus']),
    'data-sql': ('数据/SQL', ['sql', '查询', '数据', 'schema', 'ddl', '迁移']),
    'credit-risk': ('授信/风控/清分', ['授信', '风控', '提现', '放款', '还款', '清分', '逾期']),
    'frontend': ('前端', ['前端', 'admin', '页面', 'protable', 'proform', '组件']),
}
THEME = config._cfg.get('playbook_themes', default_themes)

# 默认服务名集合，用于 short() 识别所属服务；可在 config.json 中以 "service_names" 覆盖
_HC_SERVICES = tuple(config._cfg.get('service_names', (
    'hc-order', 'hc-user', 'hc-limit', 'hc-message', 'hc-third-party',
    'hc-config', 'hc-coupon', 'hc-marketing', 'hc-gateway', 'hc-callback', 'hc-job',
)))


def _playbook_out_dir(workspace: str) -> str:
    """Resolve playbook output directory for a workspace."""
    return os.path.join(workspace, '.story', 'knowledge', 'playbooks')

THEME = {
    'requirement-dev': ('需求开发', ['实现', '编码', 'feature', 'spec', 'story', 'tapd', '设计文档', '开发', '职业', '字段']),
    'debug': ('排查/Debug', ['排查', 'debug', '为什么', '报错', 'bug', '日志', '没收到', '没进', '失败', '异常']),
    'sms-marketing': ('短信/营销', ['短信', 'sms', '免息', 'mgm', '营销', '活动', '奖励']),
    'deploy': ('部署/上线', ['deploy', '部署', '上线', 'skyladder', '发版', 'nexus']),
    'data-sql': ('数据/SQL', ['sql', '查询', '数据', 'schema', 'ddl', '迁移']),
    'credit-risk': ('授信/风控/清分', ['授信', '风控', '提现', '放款', '还款', '清分', '逾期']),
    'frontend': ('前端', ['前端', 'admin', '页面', 'protable', 'proform', '组件']),
}
CODE_EXT = ('.java', '.ts', '.tsx', '.sql', '.xml', '.yml', '.yaml')

# 角色推断：按路径关键词（basename 段或目录段）匹配，优先级从上到下
ROLE_RULES = [
    ('Controller',      [r'/controller/', r'Controller\.java']),
    ('ServiceImpl',     [r'/service/impl/', r'ServiceImpl\.java']),
    ('Service(接口)',    [r'/service/[^/]+\.java']),
    ('Mapper/DAO',      [r'/mapper/', r'/dao/', r'Mapper\.java', r'Mapper\.xml']),
    ('Entity/VO/DTO',   [r'/entity/', r'/vo/', r'/dto/', r'/domain/', r'Entity\.java', r'DTO\.java', r'VO\.java']),
    ('Processor',       [r'Processor\.java', r'/processor/']),
    ('Liquidate(清分)', [r'/liquidate/', r'Liquidate']),
    ('Validator',       [r'/validator/', r'Validator\.java']),
    ('Component',       [r'/component/', r'Component\.java']),
    ('Listener/MQ',     [r'/listener/', r'/mq/', r'/consumer/', r'Listener\.java']),
    ('Config',          [r'/config/', r'Config\.java', r'\.yml', r'\.yaml']),
    ('Job(定时)',        [r'/job/', r'Job\.java', r'XxlJob']),
    ('Enum',            [r'/enums?/', r'Enum\.java']),
    ('Util',            [r'/util/', r'Util\.java', r'Utils\.java']),
]


def cmd_class(cmd):
    c = (cmd or '').strip()
    if 'cli_skyladder' in c: return 'cli_skyladder(部署)'
    if 'cli_sql' in c: return 'cli_sql(查库)'
    if 'cli_behavior' in c or 'cli_es' in c: return 'cli_behavior/es(日志)'
    if c.startswith('curl'): return 'curl(调API)'
    if c.startswith('ssh'): return 'ssh(登机)'
    if c.startswith('git'): return 'git'
    if c.startswith(('find', 'grep', 'rg')): return 'search(find/grep)'
    if c.startswith(('python', 'python3')): return 'python(脚本)'
    return None


def fail_class(text):
    """Classify a failure text; categories align with failure_mode.py for linking."""
    t = (text or '').lower()
    if 'exit code: 0' in t:
        return None  # 误标(已修)，跳过
    if 'cannot find symbol' in t or 'compile' in t or 'build fail' in t:
        return '编译/构建错误'
    if 'conflict' in t or 'merge' in t:
        return 'Git冲突/状态'
    if 'no such file' in t or 'filenotfound' in t or 'does not exist' in t:
        return '文件/路径不存在'
    if 'timeout' in t or 'timed out' in t:
        return '超时/被kill'
    if 'nullpointer' in t or 'classcast' in t or 'illegalarg' in t:
        return '类型错误'
    if 'permission' in t or 'denied' in t:
        return '权限拒绝'
    return None


# hc-all 服务名集合，用于从路径里识别所属服务
_HC_SERVICES = (
    'hc-order', 'hc-user', 'hc-limit', 'hc-message', 'hc-third-party',
    'hc-config', 'hc-coupon', 'hc-marketing', 'hc-gateway', 'hc-callback', 'hc-job',
)


def infer_role(p):
    """从路径关键词推断代码角色，返回标签或 None。"""
    for role, patterns in ROLE_RULES:
        for pat in patterns:
            if re.search(pat, p, re.IGNORECASE):
                return role
    return None


def short(p, max_len=70):
    """规整展示：hc-{服务}/关键包路径/basename。

    - 识别所属服务（hc-order 等）作为前缀，解决"哪个服务"
    - Java 资源：从 com/ys/hc/{svc}/ 之后取，去掉冗余的 src/main/java/com/ys/hc
    - 仅在超长时按"保留 basename + 前导服务/模块"截断，不再丢首字母
    - 非 Java（md/sql/yml）：取 basename + 直接父目录
    """
    p = p.replace('\\', '/')
    parts = [x for x in p.split('/') if x]
    basename = parts[-1] if parts else p

    # 1. 定位所属服务
    svc = None
    for x in parts:
        if x in _HC_SERVICES:
            svc = x
            break

    # 2. Java 资源路径：截取 com/ys/hc/{svc}/ 之后的包路径
    java_idx = None
    for i, x in enumerate(parts):
        if x in ('java', 'resources'):
            java_idx = i
            break
    if java_idx is not None:
        tail = parts[java_idx + 1:]
        # 形如 com/ys/hc/order/service/impl/X.java → 去掉前 4 段冗余包名
        if len(tail) >= 4 and tail[0] == 'com' and tail[1] == 'ys' and tail[2] == 'hc':
            pkg_tail = tail[3:]  # order/service/impl/X.java
            shown = '/'.join(pkg_tail)
        else:
            shown = '/'.join(tail)
        if svc and not shown.startswith(svc + '/'):
            shown = f"{svc}/{shown}"
        return _truncate(shown, max_len)

    # 3. 非 Java：basename + 直接父目录（如 story/1064584-xxx/spec.md）
    parent = parts[-2] if len(parts) >= 2 else ''
    shown = f"{parent}/{basename}" if parent else basename
    if svc and not shown.startswith(svc):
        # 文档类若落在服务目录下，也带上服务名
        shown = f"{svc}/{shown}"
    return _truncate(shown, max_len)


def _truncate(shown, max_len):
    """超长时保留 basename + 直接父目录 + 前导服务段，中间用 … 省略，绝不切单个字符。"""
    if len(shown) <= max_len:
        return shown
    segs = shown.split('/')
    basename = segs[-1]
    parent = segs[-2] if len(segs) >= 2 else ''
    head_segs = segs[:-2]  # basename 与 parent 之前
    # 拼回：[服务等前导段] … [parent]/[basename]
    tail = f"{parent}/{basename}" if parent else basename
    head_budget = max_len - len(tail) - 2  # 留 "…/" + tail
    if head_budget <= 6:
        return '…/' + tail
    # 从左尽量保留完整段（优先保留 hc-xxx 服务段）
    kept = []
    used = 0
    for seg in head_segs:
        add = len(seg) + (1 if kept else 0)
        if used + add > head_budget:
            break
        kept.append(seg); used += add
    head = '/'.join(kept)
    return f"{head}/…/{tail}" if head else f"…/{tail}"


def is_clean_cjk(s):
    """stories 表 title 可能是损坏的 mojibake。判断是否仍是合法可读的 CJK/ASCII 串。"""
    if not s:
        return False
    # mojibake 的特征：含大量高位控制字符（如 \xb1 \xd6 这类无法解码成中文的 latin-1 字节）
    # 合法中文应是 一-鿿；损坏串表现为 latin-1 范围字符堆积
    cjk = sum(1 for ch in s if '一' <= ch <= '鿿')
    # 若含明显 CJK 且几乎无乱码高位字节，视为可用
    high = sum(1 for ch in s if '' <= ch <= 'ɏ' and ch not in '–—‘’“”…')  # latin-1 扩展（mojibake 主力）
    return cjk >= 2 and high <= 2


def clean_title(raw):
    """标题可能损坏：尝试常见重编码修复，不行就返回 None。"""
    if not raw:
        return None
    if is_clean_cjk(raw):
        return raw.strip()
    # 尝试 utf-8 误存为 latin-1/gbk 的常见修复链
    for enc in ('latin-1', 'cp1252'):
        try:
            fixed = raw.encode(enc).decode('utf-8')
            if is_clean_cjk(fixed) or fixed.isascii():
                return fixed.strip()
        except Exception:
            pass
        try:
            fixed = raw.encode(enc).decode('gbk')
            if is_clean_cjk(fixed):
                return fixed.strip()
        except Exception:
            pass
    return None


# ---------- 聚合核心：从一组 sid 抽 files/cmds/fails ----------
def aggregate(c, sids):
    files = collections.Counter()        # lower 路径 → 次数（去重归一）
    file_roles = {}                      # lower 路径 → 角色
    file_display = {}                    # lower 路径 → 保留大小写的展示路径
    ph = ','.join('?' * len(sids))
    for (p,) in c.execute(
        f"SELECT path FROM events WHERE kind='tool' AND name IN ('Read','Grep','Glob') "
        f"AND COALESCE(path,'')<>'' AND sid IN ({ph})", sids):
        disp = p.replace('\\', '/')
        if not disp.endswith(CODE_EXT):
            continue
        key = disp.lower()
        files[key] += 1
        if key not in file_display:
            file_display[key] = disp
        if key not in file_roles:
            file_roles[key] = infer_role(key)
    cmds = collections.Counter()
    for (cmd,) in c.execute(
        f"SELECT cmd FROM events WHERE kind='tool' AND name='Bash' "
        f"AND COALESCE(cmd,'')<>'' AND sid IN ({ph})", sids):
        cls = cmd_class(cmd)
        if cls: cmds[cls] += 1
    fails = collections.Counter()
    for (t,) in c.execute(
        f"SELECT text FROM events WHERE kind='result' AND ok=0 "
        f"AND COALESCE(text,'')<>'' AND sid IN ({ph})", sids):
        fc = fail_class(t)
        if fc: fails[fc] += 1
    return files, file_display, file_roles, cmds, fails


def render_files_table(files, file_display, file_roles, top=15):
    out = ["| 文件 | 角色 | 次数 |", "|---|---|---|"]
    for key, n in files.most_common(top):
        role = file_roles.get(key) or '—'
        disp = file_display.get(key, key)
        out.append(f"| `{short(disp)}` | {role} | {n} |")
    return out


def _write_meta(path, meta):
    """Write JSON sidecar metadata for a playbook markdown.

    This allows the unified knowledge layer (packages/knowledge) to index
    playbooks without parsing markdown tables.
    """
    meta_path = path + ".json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def _write_playbooks_for_workspace(c, workspace: str, ws_tag: str):
    """Generate theme and by-story playbooks for a single workspace."""
    out_dir = _playbook_out_dir(workspace)
    out_story_dir = os.path.join(out_dir, 'by-story')
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(out_story_dir, exist_ok=True)

    sessions = list(c.execute(
        "SELECT sid, first_ucmd FROM sessions WHERE ws=? AND first_ucmd IS NOT NULL",
        (ws_tag,)
    ))
    sid_theme = {}
    for sid, fu in sessions:
        fl = (fu or '').lower()
        for theme, (_, kws) in THEME.items():
            if any(k.lower() in fl for k in kws):
                sid_theme[sid] = theme; break

    index = ["# Playbooks Index", "",
             "> 从 transcript 挖掘的历史任务上下文（按 first_ucmd 主题 / story 聚类）。"
             "文件为历史高频访问，**代码可能已变，使用前用 codegraph 核验当前状态**。", ""]
    index.append("## 按任务类型")
    n_written = 0
    for theme, (label, kws) in THEME.items():
        sids = [s for s, t in sid_theme.items() if t == theme]
        if len(sids) < 3:
            continue
        files, file_display, file_roles, cmds, fails = aggregate(c, sids)
        if not files:
            continue
        md_lines = [f"# {label} Playbook", ""]
        md_lines.append(f"> 从历史 transcript 挖掘：**{len(sids)}** 个 {ws_tag} 会话归此类"
                   f"（first_ucmd 含：{', '.join(kws[:5])}）。文件为历史高频访问，"
                   f"**代码可能已变，使用前用 codegraph 核验**。角色由路径关键词推断，仅供参考。")
        md_lines.append("")
        md_lines.append("## 必看文件 Top（历史高频访问）")
        md_lines.extend(render_files_table(files, file_display, file_roles))
        if cmds:
            md_lines.append("\n## 常用操作")
            for cls, n in cmds.most_common(8):
                md_lines.append(f"- {cls}: {n}")
        if fails:
            md_lines.append("\n## 常见失败（历史踩坑）")
            for fc, n in fails.most_common(6):
                md_lines.append(f"- {fc}: {n}")
        md_path = os.path.join(out_dir, f'{theme}.md')
        open(md_path, 'w', encoding='utf-8').write('\n'.join(md_lines))
        _write_meta(md_path, {
            "id": f"playbook:{theme}",
            "type": "playbook",
            "title": f"{label} Playbook",
            "source": "dynamic",
            "theme": theme,
            "session_count": len(sids),
            "top_files": [
                {"path": file_display[key], "role": file_roles.get(key) or "", "count": n}
                for key, n in files.most_common(15)
            ],
            "common_commands": [
                {"class": cls, "count": n}
                for cls, n in cmds.most_common(8)
            ],
            "common_failures": [
                {"category": fc, "count": n}
                for fc, n in fails.most_common(6)
            ],
        })
        index.append(f"- [{label}]({theme}.md) — {len(sids)} 会话，{len(files)} 文件")
        n_written += 1

    # ---------- by-story 粒度 ----------
    index.append("")
    index.append("## 按 Story（关联了 story_id 的会话）")
    n_story = 0
    rows = list(c.execute("""SELECT s.story_id, st.title, st.branch, st.dir_path
                             FROM (SELECT DISTINCT story_id FROM sessions
                                   WHERE ws=? AND story_id IS NOT NULL) s
                             LEFT JOIN stories st USING(story_id)""", (ws_tag,)))
    for story_id, raw_title, branch, dir_path in rows:
        story_sids = [r[0] for r in c.execute(
            "SELECT sid FROM sessions WHERE ws=? AND story_id=?", (ws_tag, story_id))]
        if len(story_sids) < 3:  # 会话太少不单列
            continue
        files, file_display, file_roles, cmds, fails = aggregate(c, story_sids)
        if not files:
            continue
        title = clean_title(raw_title) or f"Story {story_id}"
        md_lines = [f"# Story {story_id} Playbook", ""]
        md_lines.append(f"> **{title}**")
        if branch:
            md_lines.append(f"> 分支：`{branch}`")
        md_lines.append(f"> 关联会话：**{len(story_sids)}**（{ws_tag} transcript 中 story_id={story_id}）。"
                   f"文件为历史高频访问，**代码可能已变，使用前用 codegraph 核验**。")
        md_lines.append("")
        md_lines.append("## 必看文件 Top")
        md_lines.extend(render_files_table(files, file_display, file_roles))
        if cmds:
            md_lines.append("\n## 常用操作")
            for cls, n in cmds.most_common(8):
                md_lines.append(f"- {cls}: {n}")
        if fails:
            md_lines.append("\n## 常见失败（历史踩坑）")
            for fc, n in fails.most_common(6):
                md_lines.append(f"- {fc}: {n}")
        md_path = os.path.join(out_story_dir, f'{story_id}.md')
        open(md_path, 'w', encoding='utf-8').write('\n'.join(md_lines))
        _write_meta(md_path, {
            "id": f"playbook:story:{story_id}",
            "type": "playbook",
            "title": f"Story {story_id} Playbook",
            "source": "dynamic",
            "linked_story": story_id,
            "session_count": len(story_sids),
            "top_files": [
                {"path": file_display[key], "role": file_roles.get(key) or "", "count": n}
                for key, n in files.most_common(15)
            ],
            "common_commands": [
                {"class": cls, "count": n}
                for cls, n in cmds.most_common(8)
            ],
            "common_failures": [
                {"category": fc, "count": n}
                for fc, n in fails.most_common(6)
            ],
        })
        index.append(f"- [Story {story_id}](by-story/{story_id}.md) — {title}（{len(story_sids)} 会话，{len(files)} 文件）")
        n_story += 1

    open(os.path.join(out_dir, 'INDEX.md'), 'w', encoding='utf-8').write('\n'.join(index))
    print(f"[{ws_tag}] written {n_written} task playbooks + {n_story} story playbooks to {out_dir}")
    return n_written, n_story


def main():
    parser = argparse.ArgumentParser(description="Generate playbooks from transcripts")
    parser.add_argument(
        "--workspace", "-w", dest="workspaces", action="append", default=None,
        help="Target workspace path (may repeat; defaults to config.WORKSPACES)",
    )
    args = parser.parse_args()

    workspaces = args.workspaces if args.workspaces else config.WORKSPACES
    c = sqlite3.connect(DB)
    total_written = 0
    total_story = 0
    for ws in workspaces:
        ws_tag = ws_of(ws)
        try:
            nw, ns = _write_playbooks_for_workspace(c, ws, ws_tag)
            total_written += nw
            total_story += ns
        except Exception as exc:
            print(f"[{ws_tag}] skipped: {exc}")
    print(f"TOTAL {total_written} task + {total_story} story playbooks")


if __name__ == '__main__':
    main()
