"""⑩a: 从 transcript 挖"任务类型 → 必看文件/常用命令/常见失败"，反哺 hc-all/.story/knowledge/playbooks/。

按 first_ucmd 主题给 session 打标，聚合该类 session 的 events：
  - playbooks/<theme>.md      按任务类型的 playbook（文件名稳定，hc-all skill 引用）
  - playbooks/by-story/<id>.md 按 story 聚合的 playbook（仅对关联了 story_id 的 session）

v2 优化：
  1. short() 改为规整展示「服务/模块/类名」，不再丢首字母、不再硬截断
  2. 新增 by-story/ 粒度，从 stories 表取 title/branch
  3. 高频文件标注角色（Controller/ServiceImpl/Entity/...）
"""
import sqlite3, collections, re, os

DB = 'D:/github/story-lifecycle/packages/story-miner/data/transcripts.db'
OUT = 'D:/hc-all/.story/knowledge/playbooks'
OUT_STORY = os.path.join(OUT, 'by-story')

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
    t = (text or '').lower()
    if 'exit code: 0' in t: return None  # 误标(已修)，跳过
    if 'cannot find symbol' in t or 'compile' in t or 'BUILD FAIL' in t: return '编译错误'
    if 'conflict' in t or 'merge' in t: return 'Git冲突'
    if 'no such file' in t or 'filenotfound' in t or 'does not exist' in t: return '文件不存在'
    if 'timeout' in t or 'timed out' in t: return '超时'
    if 'nullpointer' in t or 'classcast' in t or 'illegalarg' in t: return '运行时异常'
    if 'permission' in t or 'denied' in t: return '权限拒绝'
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


def main():
    c = sqlite3.connect(DB)
    sessions = list(c.execute("SELECT sid, first_ucmd FROM sessions WHERE ws='hc-all' AND first_ucmd IS NOT NULL"))
    sid_theme = {}
    for sid, fu in sessions:
        fl = (fu or '').lower()
        for theme, (_, kws) in THEME.items():
            if any(k.lower() in fl for k in kws):
                sid_theme[sid] = theme; break
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(OUT_STORY, exist_ok=True)

    index = ["# Playbooks Index", "",
             "> 从 agent-transcript-miner 挖掘的历史任务上下文（按 first_ucmd 主题 / story 聚类）。"
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
        out = [f"# {label} Playbook", ""]
        out.append(f"> 从历史 transcript 挖掘：**{len(sids)}** 个 hc-all 会话归此类"
                   f"（first_ucmd 含：{', '.join(kws[:5])}）。文件为历史高频访问，"
                   f"**代码可能已变，使用前用 codegraph 核验**。角色由路径关键词推断，仅供参考。")
        out.append("")
        out.append("## 必看文件 Top（历史高频访问）")
        out.extend(render_files_table(files, file_display, file_roles))
        if cmds:
            out.append("\n## 常用操作")
            for cls, n in cmds.most_common(8):
                out.append(f"- {cls}: {n}")
        if fails:
            out.append("\n## 常见失败（历史踩坑）")
            for fc, n in fails.most_common(6):
                out.append(f"- {fc}: {n}")
        open(os.path.join(OUT, f'{theme}.md'), 'w', encoding='utf-8').write('\n'.join(out))
        index.append(f"- [{label}]({theme}.md) — {len(sids)} 会话，{len(files)} 文件")
        n_written += 1

    # ---------- by-story 粒度 ----------
    index.append("")
    index.append("## 按 Story（关联了 story_id 的会话）")
    n_story = 0
    rows = list(c.execute("""SELECT s.story_id, st.title, st.branch, st.dir_path
                             FROM (SELECT DISTINCT story_id FROM sessions
                                   WHERE ws='hc-all' AND story_id IS NOT NULL) s
                             LEFT JOIN stories st USING(story_id)"""))
    for story_id, raw_title, branch, dir_path in rows:
        story_sids = [r[0] for r in c.execute(
            "SELECT sid FROM sessions WHERE ws='hc-all' AND story_id=?", (story_id,))]
        if len(story_sids) < 3:  # 会话太少不单列
            continue
        files, file_display, file_roles, cmds, fails = aggregate(c, story_sids)
        if not files:
            continue
        title = clean_title(raw_title) or f"Story {story_id}"
        out = [f"# Story {story_id} Playbook", ""]
        out.append(f"> **{title}**")
        if branch:
            out.append(f"> 分支：`{branch}`")
        out.append(f"> 关联会话：**{len(story_sids)}**（hc-all transcript 中 story_id={story_id}）。"
                   f"文件为历史高频访问，**代码可能已变，使用前用 codegraph 核验**。")
        out.append("")
        out.append("## 必看文件 Top")
        out.extend(render_files_table(files, file_display, file_roles))
        if cmds:
            out.append("\n## 常用操作")
            for cls, n in cmds.most_common(8):
                out.append(f"- {cls}: {n}")
        if fails:
            out.append("\n## 常见失败（历史踩坑）")
            for fc, n in fails.most_common(6):
                out.append(f"- {fc}: {n}")
        open(os.path.join(OUT_STORY, f'{story_id}.md'), 'w', encoding='utf-8').write('\n'.join(out))
        index.append(f"- [Story {story_id}]({os.path.relpath(OUT_STORY, OUT)}/{story_id}.md) — {title}（{len(story_sids)} 会话，{len(files)} 文件）")
        n_story += 1

    open(os.path.join(OUT, 'INDEX.md'), 'w', encoding='utf-8').write('\n'.join(index))
    print(f"written {n_written} task playbooks + {n_story} story playbooks to {OUT}")
    print('\n'.join(index[4:]))


if __name__ == '__main__':
    main()
