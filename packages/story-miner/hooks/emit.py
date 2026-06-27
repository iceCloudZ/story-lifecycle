#!/usr/bin/env python3
"""【二期草稿 · 未启用】hook emitter:stdin JSON → common schema 行 → spool。

每个 agent 的 hook(Claude/Codex/Kimi)把事件 JSON 通过 stdin 传给本脚本,
本脚本归一化成 `miner/common.py` 的事件 schema,append 一行到 spool/<src>.jsonl。
真正的入库(去重 / 增量 / 写 transcripts.db)由 drain 批处理完成,不在本脚本做。

⚠️ 状态:骨架。字段映射、mask、spool 路径均为 TODO,未接入任何 hook 配置。
"""
import os
import sys
import json

# TODO(P2): 走 config;暂不 import miner 包,避免循环/副作用。
SPOOL_DIR = os.path.expanduser('~/.story-miner/spool')


def mask(s):
    """TODO(P2): 复用 miner.common.mask(手机号/邮箱/长数字)。"""
    return s


def canonical(event_json):
    """把某端的 hook 事件 JSON 映射成 common schema dict。

    TODO(P2): 按 src(claude/codex/kimi)+ hook 类型分别映射。返回 None 表示忽略。
      PreToolUse       -> kind='tool',  name + input(cmd/path)
      PostToolUse      -> kind='result', ok/is_error;Edit/Write 另出 kind='code'
      UserPromptSubmit -> kind='ucmd'
      PreCompact       -> kind='compact' / session meta
      Stop/SessionEnd  -> 触发 drain(不在本脚本)
    """
    src = event_json.get('src') or event_json.get('source')  # TODO: 各端字段名不同
    ev = dict(src=src, kind=None, name=None, cmd=None,
              code=None, ok=None, text=None, path=None)
    return None  # TODO(P2): 实现映射


def main():
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except Exception:
        return
    ev = canonical(event)
    if not ev:
        return
    line = json.dumps(
        {k: (mask(v) if isinstance(v, str) else v) for k, v in ev.items()},
        ensure_ascii=False,
    )
    os.makedirs(SPOOL_DIR, exist_ok=True)
    # TODO(P2): spool 切分策略(按 src / 按 session)
    with open(os.path.join(SPOOL_DIR, f"{ev.get('src') or 'unknown'}.jsonl"),
              'a', encoding='utf-8') as f:
        f.write(line + '\n')


if __name__ == '__main__':
    main()
