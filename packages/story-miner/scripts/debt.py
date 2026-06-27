"""方向5 技术债务雷达：从 transcript 写入的真实代码 diff 中扫描债务标记。

v2：改为读取 SQLite DB；只保留真实源码文件（.java/.ts/.tsx/.sql）；排除脚本/tmp/生成产物路径。
"""
import sqlite3, collections, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from miner import config  # noqa: E402
from miner.common import mask  # noqa: E402

DB = config.DB_PATH
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out', 'debt.md')

DEBT = ['TODO', 'FIXME', 'HACK', 'XXX', '暂时', '临时方案',
        '硬编码', 'workaround', '先这样', '后续优化', 'deprecated', 'MagicNumber']

SRC_EXT = ('.java', '.ts', '.tsx', '.sql')

# 排除自生成脚本 / tmp / 缓存 / 构建产物路径
EXCLUDE_PATS = [
    r'\.py$',
    r'\.md$',
    r'/\.claude/',
    r'/\.codex/',
    r'/\.kimi-code/',
    r'/\.agents/',
    r'/tmp/',
    r'/\.tmp',
    r'/scripts/',
    r'/generated/',
    r'/target/',
    r'/node_modules/',
    r'/dist/',
    r'feasibility_probe',
]
EXCLUDE_RE = re.compile('|'.join(EXCLUDE_PATS), re.IGNORECASE)


def is_real_source(path):
    p = (path or '').replace('\\', '/')
    if not p.endswith(SRC_EXT):
        return False
    if EXCLUDE_RE.search(p):
        return False
    return True


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    c = sqlite3.connect(DB)
    hits = []
    for path, code in c.execute(
        "SELECT path, code FROM events WHERE kind='code' AND COALESCE(code,'')<>''"
    ):
        if not is_real_source(path):
            continue
        low = (code or '').lower()
        for kw in DEBT:
            if kw.lower() in low:
                disp = path.replace('\\', '/')
                snip = mask(code.replace('\n', ' ')[:140])
                hits.append((kw, disp, snip))

    # 按（关键词×文件）去重
    seen = set()
    dedup = []
    for kw, path, snip in hits:
        key = (kw.lower(), path.lower())
        if key in seen:
            continue
        seen.add(key)
        dedup.append((kw, path, snip))

    by_kw = collections.Counter(kw for kw, _, _ in dedup)
    by_file = collections.Counter(path for _, path, _ in dedup)

    out = ["# 方向5 · 技术债务雷达（干净版）\n",
           "> 仅扫描**真实写入的代码 diff**(Edit/Write)，已排除 `.py/.md/tmp` 等自生成产物与临时文件。",
           "> 只关注 `.java/.ts/.tsx/.sql` 源码文件。",
           f"命中（去重 关键词×文件）: **{len(dedup)}**\n"]

    out.append("## 债务关键词分布\n")
    out.append("| 关键词 | 命中文件数 |")
    out.append("|---|---|")
    for kw, c_ in by_kw.most_common():
        out.append(f"| `{kw}` | {c_} |")

    out.append("\n## 疑似债务最多的文件 Top\n")
    out.append("| 文件 | 债务标记数 |")
    out.append("|---|---|")
    for f, c_ in by_file.most_common(20):
        out.append(f"| `{f}` | {c_} |")

    out.append("\n## 样本（去重后）\n")
    for kw, path, snip in dedup[:20]:
        out.append(f"- [{kw}] `{path}` — {snip}")

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print(f"debt: {len(dedup)} dedup hits in real source -> {OUT}")


if __name__ == '__main__':
    main()
