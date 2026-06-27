"""per-story / per-session / 未关联 token 聚合 + 效率列(cache 命中率、out/in 比)。
输出 scripts/out/story_token.md。"""
import os, sys, sqlite3, collections

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)
from miner import config

OUT_MD = os.path.join(_PROJ, 'scripts', 'out', 'story_token.md')


def aggregate(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT t.sid, s.story_id, t.input_tokens, t.output_tokens, '
        't.cache_read_tokens, t.cache_creation_tokens, t.reasoning_tokens '
        'FROM token_usage t LEFT JOIN sessions s ON s.sid = t.sid').fetchall()
    conn.close()
    per_story = collections.defaultdict(lambda: collections.Counter())
    unlinked = collections.Counter()
    for sid, story_id, inp, out, cr, cc, reas in rows:
        bucket = per_story[story_id] if story_id else unlinked
        bucket['input_tokens'] += inp or 0
        bucket['output_tokens'] += out or 0
        bucket['cache_read_tokens'] += cr or 0
        bucket['cache_creation_tokens'] += cc or 0
        bucket['reasoning_tokens'] += reas or 0
        bucket['n'] += 1
    def enrich(c):
        inp = c['input_tokens']; denom = inp + c['cache_read_tokens'] + c['cache_creation_tokens']
        c['total_tokens'] = inp + c['output_tokens'] + c['cache_read_tokens'] + c['cache_creation_tokens']
        c['cache_hit'] = (c['cache_read_tokens'] / denom) if denom else 0.0
        c['out_in_ratio'] = (c['output_tokens'] / inp) if inp else 0.0
        return c
    for k in per_story:
        enrich(per_story[k])
    enrich(unlinked)
    return per_story, unlinked


def main():
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    per_story, unlinked = aggregate(config.DB_PATH)
    out = ['# Story Token 消耗\n', '## per-story\n',
           '| story | turns | input | output | cache_read | cache_create | reasoning | total | cache_hit | out/in |',
           '|---|---|---|---|---|---|---|---|---|---|']
    for sid, c in sorted(per_story.items(), key=lambda kv: -kv[1]['total_tokens']):
        out.append(f"| {sid} | {c['n']} | {c['input_tokens']} | {c['output_tokens']} | "
                   f"{c['cache_read_tokens']} | {c['cache_creation_tokens']} | {c['reasoning_tokens']} | "
                   f"{c['total_tokens']} | {c['cache_hit']:.1%} | {c['out_in_ratio']:.2f} |")
    out += ['\n## 未关联 story 的 session(合计)\n',
            f"- turns: {unlinked['n']}  input: {unlinked['input_tokens']}  output: {unlinked['output_tokens']}  "
            f"cache_read: {unlinked['cache_read_tokens']}  total: {unlinked['total_tokens']}  "
            f"cache_hit: {unlinked['cache_hit']:.1%}"]
    with open(OUT_MD, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out))
    print(f"story_token -> {OUT_MD}")
    print(f"stories: {len(per_story)}, unlinked turns: {unlinked['n']}")


if __name__ == '__main__':
    main()
