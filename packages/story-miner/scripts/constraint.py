"""方向6 约束库建设：从 transcript 抽取高频约束，结构化后可转成 lint 规则。

v2：改为读取 SQLite DB（不再依赖硬编码 pickle），输出规则表 docs/constraint-rules.md。
"""
import sqlite3, re, collections, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from miner.common import mask  # noqa: E402

DB = 'D:/github/story-lifecycle/packages/story-miner/data/transcripts.db'
OUT_DOC = 'D:/github/story-lifecycle/packages/story-miner/docs/constraint-rules.md'

# 约束关键词（用户指令里表达强制/禁止的语气词）
KW = ['必须', '禁止', '不要', '不能', '务必', '严禁', '不可', '绝不',
      '应当', '应该', '避免', '不准', '禁止', '不要直接', '不能提交', '不要提交',
      '只准', '只能', '必须走', '禁止直接']

THEME = [
    ('分支/git', ['分支', 'commit', 'push', 'merge', 'git', 'master', 'test', 'tag']),
    ('部署/上线', ['部署', '上线', '发版', 'skyladder', 'nexus', 'rollback']),
    ('数据库/SQL', ['sql', 'ddl', '数据', '删', 'update', '库', 'schema', '迁移']),
    ('skill/流程', ['skill', '流程', '链', '跳过', 'orchestrate', 'mcp', 'provider']),
    ('配置/安全', ['nacos', '配置', '加密', 'enc', 'secret', 'password', 'token', 'ak/sk']),
    ('代码质量', ['不要', '硬编码', '中文', '英文', '占位', 'magic', '常量', '枚举']),
    ('文档/规范', ['文档', 'doc', 'README', '规范', '注释', '对客']),
    ('任务管理', ['story', 'tapd', 'bug', 'jira', '验收', '交付']),
]


def mask_cid(s):
    """约束样例导出时额外遮蔽常见 cid/uid 等短数字 ID（mask() 只覆盖 11+ 位）。"""
    # 先处理带前缀的 cid/uid，再处理裸 999 开头 ID；
    # 用 (?<!\d)(?!\d) 避免中文字符被 \b 当成 word char 导致边界失效。
    s = re.sub(r'(?<!\w)cid[:：]\d+', 'cid:****', s, flags=re.IGNORECASE)
    s = re.sub(r'(?<!\w)uid[:：]\d+', 'uid:****', s, flags=re.IGNORECASE)
    s = re.sub(r'(?<!\d)999\d{4,7}(?!\d)', '999****', s)
    return s


def load_constraints():
    c = sqlite3.connect(DB)
    sents = []
    for (t,) in c.execute("SELECT text FROM events WHERE kind='ucmd' AND text IS NOT NULL"):
        t = t or ''
        # 按常见断句符拆分，保留完整语义
        for sent in re.split(r'[。\n；!！?？]', t):
            sent = sent.strip()
            if len(sent) < 4:
                continue
            for k in KW:
                if k in sent:
                    sents.append((k, mask_cid(mask(sent[:150]))))
                    break
    # 按（关键词+前50字符）去重
    seen = set()
    dedup = []
    for k, s in sents:
        key = (k, s[:50])
        if key in seen:
            continue
        seen.add(key)
        dedup.append((k, s))
    return dedup


def cluster(dedup):
    clusters = []
    for name, kws in THEME:
        sub = [s for k, s in dedup if any(kw in s.lower() for kw in kws)]
        if sub:
            clusters.append((name, sub))
    return clusters


# 从真实约束里提炼出的、可直接 grep 执行的规则。
# 规则 = (规则文本, 检查命令, 严重级, 样例约束句, 归属主题)
EXECUTABLE_RULES = [
    (
        "doc/设计稿/中间产物不要提交进 git",
        r"rg -n '\.docx?|\.tmp|\.log|/tmp/|\.claude/tmp|\.story/.*\.json|\.pyc|node_modules' --type java --type ts -g '!**/test/**'",
        "P0",
        "doc 不要提交 git；中间产物不要提交",
        "分支/git",
    ),
    (
        "禁止在测试分支/test 环境直接改生产配置或数据库",
        r"rg -n 'nacos|application-.*\.yml|update .* set|delete from' --type java -g '!**/test/**' | rg -i 'test|staging|dev'",
        "P0",
        "不要在 test 分支直接改生产配置",
        "配置/安全",
    ),
    (
        "硬编码业务 ID / 实验 ID / token / 手机号必须清掉",
        r"rg -n 'experiment_id|activity_id|token|phone|mobile|\b\d{10,}\b' --type java --type ts -g '!**/test/**'",
        "P0",
        "禁止硬编码实验 ID、活动 ID、token",
        "代码质量",
    ),
    (
        "TODO/FIXME/HACK/占位符常量不能进生产",
        r"rg -n 'TODO|FIXME|HACK|_TEST_|_PLACEHOLDER|_DUMMY|_TEMP| System\.out|printStackTrace|console\.log' -g '!**/test/**'",
        "P0",
        "TODO/FIXME/占位符不能留到生产",
        "代码质量",
    ),
    (
        "对客可见的中文必须走 i18n（菲项对客只能是英文/菲语）",
        r"rg -n '\p{Han}' --type java -g '!**/test/**' ; rg -n '\p{Han}' frontends/hc-admin/src",
        "P0",
        "对客不要出现中文",
        "文档/规范",
    ),
    (
        "外部数据直接 Enum.valueOf 必须加防御",
        r"rg -n '\.valueOf\(' --type java -g '!**/test/**'",
        "P0",
        "外部数据 valueOf 要有 try-catch/校验",
        "代码质量",
    ),
    (
        "不要跳过 skill 流程 / MCP 链式调用约束",
        r"rg -n 'orchestrate|mcp|provider|skill' --type java --type ts -g '!**/test/**' | rg -i '跳过|bypass|直接调用'",
        "P1",
        "不要跳过 skill 流程，不要直接调用内部 MCP",
        "skill/流程",
    ),
    (
        "SQL/数据操作必须有备份或回滚说明",
        r"rg -n 'delete from|update .* set|drop table|truncate' --type sql --type java -g '!**/test/**'",
        "P1",
        "删数据/改数据前确认可回滚",
        "数据库/SQL",
    ),
]


def render_rules_table():
    out = ["| 主题 | 严重级 | 规则 | 检查命令（grep/ripgrep） | 样例来源 |",
           "|---|---|---|---|---|"]
    for rule, cmd, sev, example, theme in EXECUTABLE_RULES:
        # markdown 表格里的竖线用 HTML entity，复制出来仍是 |
        safe_rule = rule.replace('|', '&#124;')
        safe_cmd = f"`{cmd.replace('|', '&#124;')}`"
        safe_ex = example.replace('|', '&#124;')
        out.append(f"| {theme} | {sev} | {safe_rule} | {safe_cmd} | {safe_ex} |")
    return out


def main():
    os.makedirs(os.path.dirname(OUT_DOC), exist_ok=True)
    dedup = load_constraints()
    clusters = cluster(dedup)

    out = ["# 约束库规则表（从 transcript 沉淀）\n",
           "> 来源：agent-transcript-miner 中真实用户指令（ucmd）里含「必须/禁止/不要/不能」等强制语气的片段。",
           "> 本表把高频、可 grep 执行的约束沉淀为检查项，接入 `code-standards-check` skill。",
           ""]
    out.append(f"## 统计\n")
    out.append(f"- 去重约束片段：**{len(dedup)}**")
    out.append(f"- 聚类主题数：**{len(clusters)}**")
    out.append(f"- 已沉淀为 grep 可执行规则：**{len(EXECUTABLE_RULES)}** 条\n")

    out.append("## 可执行约束规则\n")
    out.extend(render_rules_table())
    out.append("")

    out.append("## 约束主题聚类（样本）\n")
    for name, sub in clusters:
        out.append(f"### {name}（{len(sub)} 条）\n")
        for s in sub[:8]:
            out.append(f"- {s}")
        out.append("")

    out.append("## 使用方式\n")
    out.append("1. 在 `code-standards-check` skill 的「速查」区按主题加入上表命令。")
    out.append("2. 提交/合并/上线前对本次变更文件跑一遍对应主题的命令，逐条核对上下文。")
    out.append("3. 命中结果按 `category | severity | file:line | 问题 | 建议` 记录。")

    with open(OUT_DOC, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print(f"constraint: {len(dedup)} constraints, {len(EXECUTABLE_RULES)} executable rules -> {OUT_DOC}")


if __name__ == '__main__':
    main()
