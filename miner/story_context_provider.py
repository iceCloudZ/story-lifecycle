"""StoryContextProvider — 给 story-lifecycle 注入历史 transcript 上下文。

只读查询 transcripts.db，为某个 story/stage 生成一段简短（<500 字）的
历史上下文摘要，注入 story-lifecycle 的 design/build/verify prompt，让
agent 复用既往调研结论、减少重复扫描代码库。

加载方式：story-lifecycle 通过 importlib 按 config 动态加载本模块（鸭子
类型，本模块**不 import story-lifecycle**）。任何异常都返回 None，绝不
阻断 prompt 渲染。

匹配策略（sessions.story_id 格式不统一：纯数字 / tapd-<id> / tapd-bug_<id>）：
  1. stories.story_id 精确 = story_key，或数字部分相等
  2. 兜底：workspace 标签（ws_of）匹配 + ts 落在 story 时间窗内
"""

import logging
import re
import sqlite3

from .common import mask, ws_of

log = logging.getLogger("miner.story_context_provider")


def _digits(s: str) -> str:
    m = re.search(r"\d+", s or "")
    return m.group(0) if m else ""


def _row_to_story(r) -> dict:
    return {
        "story_id": r[0],
        "workspace": r[1],
        "title": r[2],
        "status": r[3],
        "stage": r[4],
        "first_ts": r[5] or "",
        "last_ts": r[6] or "",
    }


def _row_to_session(r) -> dict:
    return {
        "sid": r[0],
        "src": r[1],
        "ws": r[2],
        "ts": r[3],
        "turns": r[4],
        "ntools": r[5] or 0,
        "nerrs": r[6] or 0,
        "first_ucmd": r[7] or "",
    }


class TranscriptStoryContextProvider:
    """只读查 transcripts.db，生成该 story 的历史上下文摘要。"""

    def __init__(self, config=None):
        config = config or {}
        self.db_path = config.get("db_path")
        if not self.db_path:
            try:
                from . import config as _cfg

                self.db_path = _cfg.DB_PATH
            except Exception:
                self.db_path = "data/transcripts.db"

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def get_context(self, story_key: str, workspace: str, stage: str):
        """返回历史上下文 markdown 段，或 None。永不抛异常。"""
        try:
            return self._build(story_key, workspace, stage)
        except Exception as exc:  # noqa: BLE001
            log.warning("story_context_provider failed for %s: %s", story_key, exc)
            return None

    def _build(self, story_key, workspace, stage):
        conn = self._connect()
        try:
            cur = conn.cursor()
            story = self._match_story(cur, story_key, workspace)
            if not story:
                return None
            sessions = self._find_sessions(cur, story, workspace)
            if not sessions:
                return None
            stats = self._aggregate(cur, [s["sid"] for s in sessions])
            return self._render(story, sessions, stats)
        finally:
            conn.close()

    def _match_story(self, cur, story_key, workspace):
        rows = list(
            cur.execute(
                "SELECT story_id,workspace,title,status,stage,first_ts,last_ts FROM stories"
            )
        )
        # 1. exact
        for r in rows:
            if r[0] == story_key:
                return _row_to_story(r)
        # 2. numeric part match
        d = _digits(story_key)
        if d:
            for r in rows:
                if _digits(r[0]) == d:
                    return _row_to_story(r)
        # 3. workspace tail match
        if workspace:
            ws_tail = ws_of(workspace)
            for r in rows:
                if r[1] and ws_of(r[1]) == ws_tail:
                    return _row_to_story(r)
        return None

    def _find_sessions(self, cur, story, workspace):
        out, seen = [], set()
        cols = "sid,src,ws,ts,turns,ntools,nerrs,first_ucmd"

        def _add(r):
            if r[0] not in seen:
                out.append(_row_to_session(r))
                seen.add(r[0])

        # primary: by story_id (exact)
        for r in cur.execute(
            f"SELECT {cols} FROM sessions WHERE story_id=?", (story["story_id"],)
        ):
            _add(r)
        # numeric fallback (sessions.story_id format is inconsistent)
        if not out:
            d = _digits(story["story_id"])
            if d:
                for r in cur.execute(
                    f"SELECT {cols} FROM sessions WHERE story_id LIKE ?",
                    (f"%{d}%",),
                ):
                    _add(r)
        # workspace + time-window fallback
        if not out and workspace:
            ws = ws_of(workspace)
            fts, lts = story["first_ts"][:10], story["last_ts"][:10]
            q = f"SELECT {cols} FROM sessions WHERE ws=?"
            params = [ws]
            if fts and lts:
                q += " AND ts BETWEEN ? AND ?"
                params += [fts, lts]
            q += " ORDER BY ts LIMIT 20"
            for r in cur.execute(q, params):
                _add(r)
        out.sort(key=lambda s: s["ts"] or "")
        return out[:15]

    def _aggregate(self, cur, sids):
        if not sids:
            return {"tools": {}}
        placeholders = ",".join("?" * len(sids))
        tools = {}
        for name, cnt in cur.execute(
            f"SELECT name,count(*) AS cnt FROM events WHERE sid IN ({placeholders}) "
            f"AND kind='tool' AND name IS NOT NULL GROUP BY name "
            f"ORDER BY cnt DESC LIMIT 8",
            sids,
        ):
            if name:
                tools[name] = cnt
        return {"tools": tools}

    def _render(self, story, sessions, stats):
        n = len(sessions)
        total_tools = sum(s["ntools"] for s in sessions)
        total_errs = sum(s["nerrs"] for s in sessions)
        cmds = []
        for s in sessions[:3]:
            fc = (s["first_ucmd"] or "").strip().replace("\n", " ")
            if fc:
                cmds.append(mask(fc)[:50])
        tool_str = "、".join(f"{k}×{v}" for k, v in list(stats["tools"].items())[:5]) or "无"
        title = mask(story["title"] or "")[:40]
        lines = [
            "### 历史上下文（来自既往 transcript）",
            f"- 该 story（{title}）在 {story['workspace']} 历史上有 {n} 个相关 session，"
            f"累计约 {total_tools} 次工具调用、{total_errs} 次错误。",
            f"- 常用工具：{tool_str}。",
        ]
        if cmds:
            lines.append("- 早期指令线索：" + " / ".join(cmds))
        lines.append(
            f"- 状态：{story['status']}/{story['stage']}（"
            f"{story['first_ts'][:10]}~{story['last_ts'][:10]}）。"
            "复用既往调研结论，减少重复扫描。"
        )
        return mask("\n".join(lines))[:500]
