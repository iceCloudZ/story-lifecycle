"""失败模式细化分析。

取 events where kind='result' and ok=0 and length(text)>5。
对 text 做细分类（文件不存在 / 接口404 / 编译错误 / 权限拒绝 / 超时 / 类型 / 空指针 / 网络 /
git冲突 / token失效 / 用户拒绝 / 工具未找到 等），用关键词匹配，多类取首个命中。
按 src 和 ws 分组，找"最常失败的工具 + 失败类型"组合 Top。

工具名恢复：result 事件的 name 列为 NULL，但其前一个 tool 事件携带工具名 ——
按 (sid, id) 顺序扫描，维护"本 session 最后一个 tool 事件的 name"，命中失败 result 时取该名。
"""
import os, sys, sqlite3, time, collections, re

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)
from miner import config  # noqa: E402

OUT_DIR = os.path.join(_PROJ, 'scripts', 'out')
OUT_FILE = os.path.join(OUT_DIR, 'failure_mode.md')
DOCS_DIR = os.path.join(_PROJ, 'docs')
CHECKLIST_FILE = os.path.join(DOCS_DIR, 'failure-checklist.md')


def connect():
    for attempt in range(10):
        try:
            conn = sqlite3.connect(config.DB_PATH, timeout=30)
            conn.execute('PRAGMA query_only = 1')
            return conn
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower():
                time.sleep(1)
                continue
            raise
    raise RuntimeError('DB locked after retries')


# 分类规则：顺序匹配，首个命中即归类（前面规则更"特异"）。
# 每条：(类别, [关键词], 说明)。关键词在 lower(text) 上子串匹配。
# 设计原则：把"特异性"的错误放前面，"软失败/兜底"放后面，避免大类吞掉细类。
RULES = [
    # —— 误标成功（Codex 摄入伪影：Exit code: 0 但被标 ok=0）—— 必须放最前，否则被吞
    ('误标成功(Exit=0)', ['exit code: 0', 'exit code 0\n'], 'ok=0 但实际 Exit code 0（ingest 误标，非真失败）'),
    # —— 用户主动拒绝 / 中断 ——
    ('用户拒绝/中断', ["doesn't want to proceed", 'tool use was rejected',
                   'rejected', 'interrupted', 'user declined'], '用户拒绝工具调用或中断'),
    # —— 工具前置约束（未先 Read / 字符串未找到）—— 特异性高，前置于通用错误
    ('工具前置约束', ['has not been read yet', 'read it first',
                  'string to replace not found', 'no changes to',
                  'already exists', 'is not empty',
                  'agent type', 'not found. available agents'], 'Edit/Write/Agent 前置条件未满足'),
    # —— 认证 / token 失效 ——
    ('Token/认证失效', ['token expired', 'token invalid', 'unauthorized',
                    'authentication failed', 'please log in', 'login to refresh',
                    'not authenticated', '401'], '登录态/token 过期'),
    # —— 权限拒绝 ——
    ('权限拒绝', ['permission denied', 'access denied', 'forbidden',
              '403', 'operation not permitted', 'eacces'], '系统/接口权限不足'),
    # —— 网络（细化：refused / reset / dns / connrefused）——
    ('网络', ['econnrefused', 'connection refused', 'econnreset', 'connection reset',
           'enotfound', 'could not resolve host', 'network is unreachable',
           'temporarily unavailable', 'connection aborted',
           '502 bad gateway', '503 service', '504 gateway',
           'etimedout', 'graphql: could not resolve'], '网络层错误'),
    # —— 接口 404 / 路由 / 资源不存在 ——
    ('接口404/路由', ['404', 'not found page', 'no such endpoint',
                  'route not found', 'cannot find module',
                  'could not resolve to a node', 'could not resolve to'], 'HTTP/路由 404 或资源未找到'),
    # —— 文件/路径不存在 ——
    ('文件/路径不存在', ['file does not exist', 'no such file',
                    'file not found', 'cannot access', "does not exist",
                    'no such file or directory', 'path does not exist',
                    'errno 2', 'filenotfounderror'], '读/写/访问不存在的文件或路径'),
    # —— 超时（命令级：124=timeout, 143=SIGTERM, 137=OOM-kill）——
    ('超时/被kill', ['timed out', 'timeout', 'deadline exceeded',
                  'exit code 124', 'exit code 137', 'exit code 143',
                  'exit code: 124', 'exit code: 137', 'exit code: 143'], '命令超时或被 SIGTERM/OOM kill'),
    # —— git：冲突 / unknown revision / nothing to commit / 状态 ——
    ('Git冲突/状态', ['conflict', 'merge conflict', '<<<<<<<',
                  'unknown revision', 'ambiguous argument',
                  "nothing to commit", 'your local changes',
                  'would be overwritten', 'fatal: path',
                  'fatal: not a git repository', 'exit code 128',
                  'failed: git pull', 'failed: git push', 'failed: git stash',
                  'failed: git checkout', 'failed: git merge'], 'git 操作冲突或状态错误'),
    # —— Spring/Bean 编译（Java 特定，放通用编译前）——
    ('编译/构建错误', ['error:', 'cannot find symbol', '找不到符号',
                   'compilation failed', 'build failed', 'build success',
                   'syntax error', 'parseerror', 'parse error',
                   'unexpected token', 'mvn ', 'gradle ', 'tsc ',
                   'error ts', 'javac', 'unresolved reference',
                   'nosuchbeandefinition', 'bean ', 'go build',
                   'validation failed', 'maven', 'compilation'], '语言/构建编译失败'),
    # —— 空指针 / None ——
    ('空指针/None', ['nullpointerexception', 'none is not',
                  "attributeerror: 'nonetype'", 'is null',
                  'null reference', 'nil pointer'], '空值/None 访问'),
    # —— 类型错误 ——
    ('类型错误', ['typeerror', 'classcastexception', 'type mismatch',
              'argument of type', 'invalid type', 'classcast'], '类型不匹配'),
    # —— 参数/用法错误（含 exit code 2 = argparse 失败）——
    ('参数/用法错误', ['usage:', 'unrecognized arguments', 'unrecognized argument',
                   'invalid argument', 'invalid parameter',
                   'missing required', 'inputvalidationerror',
                   'the following issues', 'exit code 2',
                   'taskkill', 'invalid switch', '无效参数'], 'CLI/工具参数错误'),
    # —— 工具链未找到（rg/rtk/npm module 等）——
    ('工具链缺失', ['binary ', 'not found on path', 'not on path',
                'command not found', 'no module named', 'failed to resolve',
                'rtk: failed', 'skills repo missing', 'npm warn eresolve',
                'npm err', 'peer dependency'], '依赖二进制/模块缺失'),
    # —— Python 异常 ——
    ('Python异常', ['traceback (most recent call last)', 'syntaxerror',
                'indentationerror', 'modulenotfounderror', 'attributeerror',
                'keyerror', 'valueerror', 'indexerror',
                'python ', 'py ', '.py'], '通用 Python 异常'),
    # —— 软失败：命令非零退出但产出可读输出（多为脚本里 set -e 或断言）——
    # 同时覆盖 Codex 的 "Exit code: 1"（带冒号）格式
    ('命令非零退出(软)', ['exit code 1', 'exit code 7', 'exit code 49',
                    'exit code 66', 'exit code 123', 'exit code 129',
                    'exit code: 1', 'exit code: 7', 'exit code: 49',
                    'exit code: 66', 'exit code: 123', 'exit code: 129',
                    'exit code: 2', 'exit code: 3', 'exit code: 4',
                    'exit code: 5', 'exit code: 6', 'exit code: 8'], '命令非零退出但无明确错误模式（多为脚本断言/流控）'),
]


def classify(text):
    if not text:
        return '其他'
    low = text.lower()
    for cat, pats, _ in RULES:
        for p in pats:
            if p.startswith('re:'):
                if re.search(p[3:], low):
                    return cat
            elif p in low:
                return cat
    return '其他'


def write_checklist(total, by_cat, by_tool_cat, by_src_cat, by_src_ws_tool_cat):
    """把高频失败模式转成可复用的避坑检查项，写入 docs/failure-checklist.md。"""
    os.makedirs(DOCS_DIR, exist_ok=True)

    def get_count(*keys):
        return by_tool_cat.get(keys, 0)

    def cat_count(cat):
        return by_cat.get(cat, 0)

    # 按端聚合的 Top 失败类型（用于上下文）
    src_top_cat = {}
    for (src, cat), n in by_src_cat.items():
        src_top_cat.setdefault(src, []).append((cat, n))
    for src in src_top_cat:
        src_top_cat[src].sort(key=lambda x: -x[1])

    # 检查项：检查项文本/针对的失败模式/数据来源/宿主 skill/自动 or 人工/检查时机
    items = [
        {
            'check': 'Edit/Write 文件前先 Read',
            'target': '工具前置约束（Edit 未读先写）',
            'data': f"Edit × 工具前置约束 {get_count('Edit', '工具前置约束')} 次（全端）",
            'skill': 'build-check（本地即时拦截）/ pre-release-review（最终把关）',
            'auto': '可自动：检测 Edit/Write 前同 session 是否存在对应 Read',
            'when': '每次 Edit/Write 调用前',
        },
        {
            'check': '编译/构建前清理旧产物并确认依赖版本',
            'target': '编译/构建错误',
            'data': f"编译/构建错误 {cat_count('编译/构建错误')} 次；Bash×编译 {get_count('Bash', '编译/构建错误')}、codegraph_context×编译 {get_count('codegraph_context', '编译/构建错误')}、shell_command×编译 {get_count('shell_command', '编译/构建错误')}、Write×编译 {get_count('Write', '编译/构建错误')}",
            'skill': 'build-check',
            'auto': 'CI 自动：mvn clean install / tsc --noEmit / yarn build',
            'when': '本地修改后、PR 合并前',
        },
        {
            'check': 'Bash 命令失败后先解析退出码与 stderr，再决定重试或修复',
            'target': '命令非零退出(软)',
            'data': f"命令非零退出(软) {cat_count('命令非零退出(软)')} 次，占全部失败 {cat_count('命令非零退出(软)')/total*100:.1f}%；shell_command {get_count('shell_command', '命令非零退出(软)')}、Bash {get_count('Bash', '命令非零退出(软)')}",
            'skill': 'build-check',
            'auto': '半自动：捕获 exit code，强制要求查看最近 20 行 stderr',
            'when': '脚本/命令返回非零后',
        },
        {
            'check': '提交/编译前处理 Git 冲突并拉取最新代码',
            'target': 'Git冲突/状态',
            'data': f"Git冲突/状态 {cat_count('Git冲突/状态')} 次；Bash×Git冲突 {get_count('Bash', 'Git冲突/状态')} 次，多集中在 hc-all",
            'skill': 'build-check',
            'auto': '可自动：git status --porcelain 检查无 UU/AA/DD 冲突标记',
            'when': '本地构建前、提交前、发版前',
        },
        {
            'check': 'Python 脚本运行前检查虚拟环境与依赖',
            'target': 'Python异常',
            'data': f"Python异常 {cat_count('Python异常')} 次；Bash×Python异常 {get_count('Bash', 'Python异常')}、shell_command×Python异常 {get_count('shell_command', 'Python异常')}",
            'skill': 'build-check',
            'auto': '可自动：import 检查、requirements 对齐',
            'when': '运行 .py 脚本或 CI 步骤前',
        },
        {
            'check': 'Read/Bash/cat 访问文件前先校验路径存在性',
            'target': '文件/路径不存在',
            'data': f"文件/路径不存在 {cat_count('文件/路径不存在')} 次；shell_command {get_count('shell_command', '文件/路径不存在')}、Bash {get_count('Bash', '文件/路径不存在')}、Read {get_count('Read', '文件/路径不存在')}",
            'skill': 'build-check',
            'auto': '部分自动：静态路径存在性检查',
            'when': '访问非当前目录文件前',
        },
        {
            'check': '长耗时/批量命令加 timeout 与重试策略',
            'target': '超时/被kill',
            'data': f"超时/被kill {cat_count('超时/被kill')} 次；shell_command {get_count('shell_command', '超时/被kill')}、Bash {get_count('Bash', '超时/被kill')}",
            'skill': 'build-check',
            'auto': '可自动：timeout 命令包装、CI timeout 配置',
            'when': '执行测试、批量脚本、远程命令时',
        },
        {
            'check': '破坏性/敏感操作前显式向用户确认',
            'target': '用户拒绝/中断',
            'data': f"用户拒绝/中断 {cat_count('用户拒绝/中断')} 次；Bash×用户拒绝 {get_count('Bash', '用户拒绝/中断')} 次",
            'skill': 'pre-release-review',
            'auto': '人工/流程：git push -f、drop table、删除分支、覆盖配置前必须确认',
            'when': '发布前 review、危险命令执行前',
        },
        {
            'check': 'CLI/工具参数变更后核对 usage',
            'target': '参数/用法错误',
            'data': f"参数/用法错误 {cat_count('参数/用法错误')} 次；Bash×参数错误 {get_count('Bash', '参数/用法错误')} 次",
            'skill': 'build-check',
            'auto': '半自动：变更脚本后跑一次 --help / dry-run',
            'when': '修改脚本入参或调用新工具时',
        },
        {
            'check': 'Token/认证过期前主动刷新或使用长期凭证',
            'target': 'Token/认证失效',
            'data': f"Token/认证失效 {cat_count('Token/认证失效')} 次；Bash×Token失效 {get_count('Bash', 'Token/认证失效')} 次",
            'skill': 'build-check / pre-release-review',
            'auto': '可自动：检测 401/403 后触发重新登录',
            'when': '调用需登录态接口前、流水线配置时',
        },
    ]

    out = []
    out.append('# 失败模式 → 避坑检查项\n')
    out.append('> 来源：`scripts/failure_mode.py` 对 `events where kind=\'result\' and ok=0 and length(text)>5` 的细化分析。')
    out.append(f'> 样本：共 **{total}** 条失败记录，覆盖主要端和工作区。')
    out.append('> 用途：把高频失败转成可执行的预防检查项，建议挂载到 `pre-release-review` 或 `build-check` skill。\n')

    out.append('## 核心失败分布（用于优先级排序）\n')
    out.append('| 失败类型 | 次数 | 占比 |')
    out.append('|---|---|---|')
    for cat, n in by_cat.most_common(10):
        out.append(f'| {cat} | {n} | {n/total*100:.1f}% |')
    out.append('')

    out.append('## 预防检查项\n')
    out.append('按出现频次和可预防性排序，建议优先落地前 5 项。\n')
    out.append('| 优先级 | 检查项 | 针对的失败模式 | 数据依据 | 建议宿主 skill | 自动/人工 | 检查时机 |')
    out.append('|---|---|---|---|---|---|---|')
    for i, it in enumerate(items, 1):
        out.append(
            f"| {i} | {it['check']} | {it['target']} | {it['data']} | {it['skill']} | {it['auto']} | {it['when']} |"
        )
    out.append('')

    out.append('## 高频「工具 × 失败类型」组合 Top 10\n')
    out.append('> 这些组合是检查项设计的直接输入。\n')
    out.append('| 排名 | 工具 | 失败类型 | 次数 | 占比 | 建议检查项 |')
    out.append('|---|---|---|---|---|---|')
    rank = 1
    for (tool, cat), n in by_tool_cat.most_common(10):
        suggestion = ''
        if tool == 'Edit' and cat == '工具前置约束':
            suggestion = 'Edit 前先 Read'
        elif cat == 'Git冲突/状态':
            suggestion = '提交前处理冲突、拉最新'
        elif cat == '编译/构建错误':
            suggestion = '构建前清理、依赖对齐'
        elif cat == '命令非零退出(软)':
            suggestion = '失败后解析 stderr'
        elif cat == 'Python异常':
            suggestion = '检查 venv/依赖/PYTHONPATH'
        elif cat == '文件/路径不存在':
            suggestion = '访问前校验路径'
        elif cat == '用户拒绝/中断':
            suggestion = '敏感操作前确认'
        elif cat == '参数/用法错误':
            suggestion = '变更后核对 usage'
        out.append(f'| {rank} | {tool} | {cat} | {n} | {n/total*100:.1f}% | {suggestion} |')
        rank += 1
    out.append('')

    out.append('## Skill 接入建议\n')
    out.append('### build-check')
    out.append('`build-check` 是"代码变更后、提交/部署前的编译验证"skill，最适合承载**可机械执行的本地/CI 检查项**：')
    out.append('- 新增检查项 1、2、3、5、6、7、9：Edit 前 Read、构建前清理、失败后解析 stderr、Python 环境、路径存在性、timeout、参数核对。')
    out.append('- 实现方式：在 skill 的"验证要点"后追加一个 `## 失败模式拦截清单` 小节，列出上述 7 项，并给出可执行的 bash/python 片段。\n')

    out.append('### pre-release-review')
    out.append('`pre-release-review` 是"发布前综合检查"skill，最适合承载**需要人工判断的发布级检查项**：')
    out.append('- 新增检查项 8：破坏性/敏感操作前显式确认（git push -f、drop table、删除分支、覆盖配置）。')
    out.append('- 新增检查项 10：Token/认证凭证在流水线中的有效期与刷新策略。')
    out.append('- 实现方式：在 `## Step 3: Run 4 Checks` 之后追加 `### Check 5: 失败模式 Review`，按本清单逐项打勾，阻塞项必须确认。\n')

    out.append('## 未覆盖/待细化项\n')
    out.append('- `误标成功(Exit=0)` 等 Codex ingest 伪影已在 `failure_mode.py` 规则最前排重，不进入业务检查项。')
    out.append('- `其他` 类型（45 次，2.8%）未命中任何关键词，需要后续人工抽样细化。')
    out.append('- `接口404/路由`（7 次）和 `权限拒绝`（11 次）样本较少，暂不单独列检查项，可并入通用"接口调用前校验"项。\n')

    out.append('---\n')
    out.append(f'*Generated by scripts/failure_mode.py at {time.strftime("%Y-%m-%d %H:%M:%S")}*')

    with open(CHECKLIST_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print(f'[failure_mode] wrote {CHECKLIST_FILE}')


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = connect()
    c = conn.cursor()

    # 取所有事件按 (sid, id) 顺序，恢复每个 result 前最近的 tool name
    # 只需要 result 与 tool 两类；为内存安全用流式 fetch
    c.execute(
        "SELECT id, sid, kind, name, ok, text FROM events "
        "WHERE kind IN ('tool','result') ORDER BY sid, id"
    )
    last_tool_by_sid = {}      # sid -> last seen tool name
    failures = []              # (src, ws, tool, category, text)
    for _id, sid, kind, name, ok, text in c.fetchall():
        if kind == 'tool':
            last_tool_by_sid[sid] = name
            continue
        # result
        if ok == 0 and text and len(text) > 5:
            tool = last_tool_by_sid.get(sid)
            cat = classify(text)
            failures.append((sid, tool, cat))

    # 失败记录本身没有 src/ws（避免再一次 join），用 sessions 表回填
    sid2srcws = {}
    c.execute("SELECT sid, src, ws FROM sessions")
    for sid, src, ws in c.fetchall():
        sid2srcws[sid] = (src, ws)

    enriched = []
    for sid, tool, cat in failures:
        src, ws = sid2srcws.get(sid, ('?', '?'))
        enriched.append((src, ws, tool or '(unknown)', cat))

    total = len(enriched)
    by_cat = collections.Counter(r[3] for r in enriched)
    by_src = collections.Counter(r[0] for r in enriched)
    by_src_cat = collections.Counter((r[0], r[3]) for r in enriched)
    by_ws_cat = collections.Counter((r[1], r[3]) for r in enriched)
    by_tool_cat = collections.Counter((r[2], r[3]) for r in enriched)
    by_src_ws_tool_cat = collections.Counter((r[0], r[1], r[2], r[3]) for r in enriched)

    out = []
    out.append('# 失败模式细化分析（result 事件 ok=0）\n')
    out.append(f'> 取 `events where kind=\'result\' and ok=0 and length(text)>5`，共 **{total}** 条。')
    out.append('> 工具名通过"同 session 内最近一个 tool 事件"恢复（result.name 为 NULL）。')
    out.append('> 失败类型用关键词匹配，多类取首个命中（规则按特异性排序）。\n')

    # 1. 失败类型总分布
    out.append('## 1. 失败类型总分布\n')
    out.append('| 失败类型 | 次数 | 占比 |')
    out.append('|---|---|---|')
    for cat, n in by_cat.most_common():
        pct = (n / total * 100) if total else 0
        out.append(f'| {cat} | {n} | {pct:.1f}% |')
    out.append('')

    # 2. 按端
    out.append('## 2. 按端（src）的失败类型分布\n')
    out.append('每端失败总数与 Top 失败类型。')
    out.append('')
    out.append('| 端 | 失败总数 | Top1 类型(次数) | Top2 类型(次数) | Top3 类型(次数) |')
    out.append('|---|---|---|---|---|')
    for src, _ in by_src.most_common():
        cats = [(cat, n) for (s, cat), n in by_src_cat.items() if s == src]
        cats.sort(key=lambda x: -x[1])
        tops = cats[:3] + [('—', 0)] * (3 - len(cats))
        cells = ' | '.join(f'{cn}({ct})' for cn, ct in tops)
        out.append(f'| {src} | {by_src[src]} | {cells} |')
    out.append('')

    # 按端的完整类型表
    out.append('### 2.1 各端 × 失败类型明细\n')
    all_cats = [c for c, _ in by_cat.most_common()]
    out.append('| 端 | ' + ' | '.join(all_cats) + ' | 合计 |')
    out.append('|---|' + '---|' * (len(all_cats) + 1))
    for src, _ in by_src.most_common():
        cells = [str(by_src_cat.get((src, cat), 0)) for cat in all_cats]
        out.append(f'| {src} | ' + ' | '.join(cells) + f' | {by_src[src]} |')
    out.append('')

    # 3. 按工作区
    out.append('## 3. 按工作区（ws）的失败类型分布\n')
    out.append('| 工作区 | 失败总数 | Top1 类型(次数) | Top2 类型(次数) | Top3 类型(次数) |')
    out.append('|---|---|---|---|---|')
    ws_tot = collections.Counter(r[1] for r in enriched)
    for ws, _ in ws_tot.most_common():
        cats = [(cat, n) for (w, cat), n in by_ws_cat.items() if w == ws]
        cats.sort(key=lambda x: -x[1])
        tops = cats[:3] + [('—', 0)] * (3 - len(cats))
        cells = ' | '.join(f'{cn}({ct})' for cn, ct in tops)
        out.append(f'| {ws} | {ws_tot[ws]} | {cells} |')
    out.append('')

    # 4. Top 工具 × 失败类型组合（核心问题）
    out.append('## 4. 最常失败的「工具 × 失败类型」组合 Top 20\n')
    out.append('| 排名 | 工具 | 失败类型 | 次数 | 占比 |')
    out.append('|---|---|---|---|---|')
    for i, ((tool, cat), n) in enumerate(by_tool_cat.most_common(20), 1):
        pct = (n / total * 100) if total else 0
        out.append(f'| {i} | {tool} | {cat} | {n} | {pct:.1f}% |')
    out.append('')

    # 5. Top 端 × 工作区 × 工具 × 类型（细粒度）
    out.append('## 5. 最常失败的「端 × 工作区 × 工具 × 类型」组合 Top 20\n')
    out.append('| 排名 | 端 | 工作区 | 工具 | 失败类型 | 次数 |')
    out.append('|---|---|---|---|---|---|')
    for i, ((src, ws, tool, cat), n) in enumerate(by_src_ws_tool_cat.most_common(20), 1):
        out.append(f'| {i} | {src} | {ws} | {tool} | {cat} | {n} |')
    out.append('')

    # 6. 每端最突出组合（一句话洞察）
    out.append('## 6. 每端"头号失败组合"\n')
    out.append('| 端 | 头号工具 | 头号失败类型 | 次数 | 占该端失败比 |')
    out.append('|---|---|---|---|---|')
    for src, _ in by_src.most_common():
        sub = [(t, n) for (s, w, t, cat), n in by_src_ws_tool_cat.items() if s == src]
        # 按 tool 维度聚合
        tool_tot = collections.Counter()
        for (s, w, t, cat), n in by_src_ws_tool_cat.items():
            if s == src:
                tool_tot[t] += n
        top_tool = tool_tot.most_common(1)[0] if tool_tot else ('—', 0)
        cat_sub = collections.Counter()
        for (s, cat), n in by_src_cat.items():
            if s == src:
                cat_sub[cat] = n
        top_cat = cat_sub.most_common(1)[0] if cat_sub else ('—', 0)
        denom = by_src[src] or 1
        out.append(
            f'| {src} | {top_tool[0]} | {top_cat[0]} | 工具{top_tool[1]}/类型{top_cat[1]} '
            f'| {top_cat[1] / denom * 100:.1f}% |'
        )
    out.append('')

    conn.close()
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print(f'[failure_mode] wrote {OUT_FILE}; total failures = {total}')
    print('=== failure_mode top categories ===')
    for cat, n in by_cat.most_common(8):
        print(f'  {cat}: {n} ({(n/total*100) if total else 0:.1f}%)')
    print('=== top tool x cat (top 6) ===')
    for (tool, cat), n in by_tool_cat.most_common(6):
        print(f'  {tool} | {cat}: {n}')

    write_checklist(total, by_cat, by_tool_cat, by_src_cat, by_src_ws_tool_cat)


if __name__ == '__main__':
    main()
