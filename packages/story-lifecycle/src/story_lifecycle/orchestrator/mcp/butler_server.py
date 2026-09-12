"""管家 MCP server(DESIGN-story-butler §3.2,WP2)—— zcode 会话里的「管家嘴和手」。

**定位**(§0 已拍板,不再讨论):agent 只管家不写码 —— 本工具面没有任何代码执行
能力(不写文件/不跑 shell);DB 是唯一事实源,所有推进经 serve 的同一 API 落库,
``confirmed_via`` 标记来源;终态/发布类(上线/结项)确认一律拒绝 —— 高危只在桌面
评审 skill 以完整上下文做(§3.6)。

**实现模式**(照 ``clarify_server.py`` 的路数):
- 可单测的纯核心(``tool_*`` 六个函数,HTTP 调用层经参数注入) + 薄 stdio
  JSONRPC 循环(``run_server``,I/O 层)。
- **全部经 HTTP 调 serve 的现有 REST API(默认 ``http://127.0.0.1:8180``,env
  ``STORY_SERVE_URL`` 可覆盖),绝不 import story_lifecycle 内部业务模块** ——
  serve 必须在跑;连不上时工具返回友好中文错误(「管家后端未启动,请先 story
  serve」),绝不抛异常炸宿主会话(§5 降级矩阵)。

**工具目录 v1(七个,method 名下划线风格)**:

| method | 参数 | 语义 |
|---|---|---|
| ``story_list`` | status? | 活跃 story 摘要(key/标题/lifecycle 状态/当前阶段/是否停确认门) |
| ``story_detail`` | key | 全量:状态/stage 进度/judge 摘要/确认门详情(含 targetState)/下一步建议/证据指针 |
| ``patrol_summary`` | - | 生产巡检发现 + stuck 状态 + 各 story reject 预算余量 |
| ``story_advance`` | key, confirm_token, confirmed_via="desktop" | **服务端执法**后推进确认门 |
| ``plan_confirm`` | key, confirm_token | **同款执法**后确认规划(POST /plan/confirm) |
| ``session_register`` | key, stage, adapter, session_id | sessions 簿记(zcode 会话登记,断点续跑有据可查) |
| ``knowledge_search`` | q, story_key?, top_k? | 知识库检索(场景/打法/踩坑/RUN 新坑;只读,GET /api/knowledge/search) |

**confirm_token 执法规则**(§3.2,服务端裁决,LLM 拼 token = 读过详情的凭据):
1. ``confirm_token`` 必须**精确等于** ``f"{story_key}:{target_state}"`` ——
   target_state 以 ``story_detail`` 返回的确认门 ``targetState`` 为准;不等 → 拒绝
   并在拒绝文案里说明正确拼法。
2. **终态类 target 一律拒绝**(``targetIsTerminal=True``,即 上线/结项 这类发布/
   终态跃迁;终态集合由 serve 侧按 UPGRADE_STATES 判定返回,butler 不本地硬编码)
   —— 拒绝文案指向桌面评审 skill / Story 详情页 UI。

**zcode 侧手动配置**(clarify 无 CLI 接线,本模块同样只提供函数):

.. code-block:: python

    import sys
    from story_lifecycle.orchestrator.mcp.butler_server import write_butler_mcp_config

    write_butler_mcp_config(r"<工作区>/.mcp.json", sys.executable)

等价的手工 JSON(merge 语义:只加/改 ``butler`` 键,clarify 的 ``lifecycle`` 等
既有 server 条目原样保留):

.. code-block:: json

    {"mcpServers": {"butler": {
        "command": "<python>",
        "args": ["-m", "story_lifecycle.orchestrator.mcp.butler_server"]}}}

启动验证:``echo {"jsonrpc":"2.0","id":1,"method":"tools/list"} | python -m
story_lifecycle.orchestrator.mcp.butler_server``(先 ``story serve``)。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

# serve 默认地址(桌面 localhost;env STORY_SERVE_URL 覆盖 —— §2 拓扑,MCP 在桌面消费)
DEFAULT_SERVE_URL = "http://127.0.0.1:8180"

# confirmed_via 合法值(DESIGN-story-butler §3.1 账本约定,与 serve 端
# routers/lifecycle._VALID_CONFIRMED_VIA 同一集合 —— API 契约,非内部实现)。
VALID_CONFIRMED_VIA = ("desktop", "wechat", "ui", "api")

# reject 预算默认值(展示用;真实上限在 serve 端 evaluation/reject_budget.py,
# 默认 3、env STORY_REJECT_LIMIT 可覆盖 —— butler 只按默认值算「余量」展示)。
DEFAULT_REJECT_BUDGET = 3

# 单次 patrol_summary 逐 story 拉时间线/巡检详情的上限(localhost 也要有界)。
_PATROL_DETAIL_LIMIT = 20
_PATROL_FAIL_DETAIL_LIMIT = 10

# judge 决策摘要/story 列表等返回的条数上限(给 LLM 读,贵在精不在多)。
_RECENT_DECISIONS = 5


class ButlerServeError(RuntimeError):
    """serve 调用失败(连接不上 / 超时)。工具层捕获后转「后端未启动」友好文案。"""


class ButlerHttpError(ButlerServeError):
    """serve 返回了 HTTP >= 400(后端在,但这次请求被拒 —— 如 404 查无 story)。

    与连接层失败(ButlerServeError 本类)分开:两者给 LLM 的提示不同 ——
    连接失败才说「先 story serve」,HTTP 错误应原样转述(如 409 成果物 gate 未满足)。
    """

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail or ""
        super().__init__(f"serve 返回 HTTP {status}: {self.detail}".strip())


def serve_base_url() -> str:
    """serve 基地址(env ``STORY_SERVE_URL`` > 默认 ``http://127.0.0.1:8180``)。"""
    return (os.environ.get("STORY_SERVE_URL") or DEFAULT_SERVE_URL).rstrip("/")


def backend_down_result() -> dict:
    """serve 连不上时的统一友好返回(§5 降级矩阵:报友好错误,不炸会话)。"""
    return {
        "ok": False,
        "summary": (
            "管家后端未启动,请先 `story serve`(默认 127.0.0.1:8180,"
            "可用环境变量 STORY_SERVE_URL 覆盖)后再调用本工具。"
        ),
    }


# ---- HTTP 层(唯一 I/O 面;测试 monkeypatch http_json 或注入 fetch/send) ----


def http_json(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    base_url: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, object]:
    """一次 HTTP 调用,返回 ``(status, 解析后的 JSON)``。

    4xx/5xx 也正常返回 status(调用方按语义处理);连接层失败(serve 不在/
    超时)抛 :class:`ButlerServeError`。标准库 urllib,零第三方依赖。
    """
    url = f"{base_url or serve_base_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
    except urllib.error.HTTPError as e:  # 4xx/5xx:读 body(FastAPI 的 detail 有用)
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        status = int(e.code)
    except Exception as e:  # URLError/ConnectionError/timeout → 后端不在
        raise ButlerServeError(f"serve 调用失败({method} {path}): {e}") from e
    parsed: object = None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
    return status, parsed


# fetch/send 的可注入形态(纯核心单测不碰网络):
#   FetchFn: (path) -> parsed JSON;HTTP>=400 抛 ButlerServeError
#   SendFn:  (method, path, body) -> (status, parsed JSON)
FetchFn = Callable[[str], object]
SendFn = Callable[[str, str, dict | None], tuple[int, object]]


def make_fetch(base_url: str | None = None) -> FetchFn:
    """构造 GET 读取函数(>=400 视为错误抛出;连接失败抛 ButlerServeError)。"""

    def _get(path: str):
        status, parsed = http_json("GET", path, base_url=base_url)
        if status >= 400:
            raise ButlerHttpError(status, _detail_of(parsed))
        return parsed

    return _get


def make_send(base_url: str | None = None) -> SendFn:
    """构造变更调用函数(透传 status,由工具层按语义转文案)。"""

    def _send(method: str, path: str, body: dict | None = None):
        return http_json(method, path, body=body, base_url=base_url)

    return _send


def _detail_of(parsed: object) -> str:
    """从 serve 的 JSON 错误体里抠 detail(FastAPI 的 HTTPException 形状)。"""
    if isinstance(parsed, dict):
        return str(parsed.get("detail", ""))
    if isinstance(parsed, str):
        return parsed
    return ""


def _http_error_text(status: int, parsed: object) -> str:
    return f"serve 返回 HTTP {status}: {_detail_of(parsed)}".strip()


def _best_effort(fetch: FetchFn, path: str, default):
    """读辅助端点,失败不炸(返回 default)—— 详情聚合允许多端点部分缺失。"""
    try:
        return fetch(path)
    except Exception:  # noqa: BLE001 — 证据聚合是锦上添花,主数据不在这
        return default


def _fetch_story_or_none(fetch: FetchFn, key: str) -> dict | None:
    """拉 story 详情;404(查无此 story)→ None,其余异常照抛。"""
    try:
        story = fetch(f"/api/story/{urllib.parse.quote(key)}")
    except ButlerHttpError as e:
        if e.status == 404:
            return None
        raise
    return story if isinstance(story, dict) else {}


# ---- confirm_token 执法(纯函数;§3.2 服务端裁决的核心) ----


def build_confirm_token(story_key: str, target_state: str) -> str:
    """confirm_token 的唯一合法拼法(文档化契约,故事详情里有提示文案)。"""
    return f"{story_key}:{target_state}"


def _token_refusal(story_key: str, gate: dict) -> dict:
    """token 不匹配的拒绝结果(说明正确拼法,给 LLM 自纠)。"""
    expected = build_confirm_token(story_key, str(gate.get("targetState") or ""))
    return {
        "ok": False,
        "refused": "token_mismatch",
        "summary": (
            f"confirm_token 不匹配,已拒绝。正确拼法:confirm_token = "
            f'"{expected}"(story_key + ":" + 确认门的 targetState,targetState 以 '
            f"story_detail 返回为准)。"
        ),
        "expectedToken": expected,
        "gate": gate,
    }


def _terminal_refusal(story_key: str, gate: dict) -> dict:
    """终态/发布类 target 的拒绝结果(高危只在桌面评审 skill / UI 做,§3.2)。"""
    return {
        "ok": False,
        "refused": "terminal_target",
        "summary": (
            f"目标态「{gate.get('targetState')}」是终态/发布类跃迁,MCP 工具面一律"
            f"拒绝 —— 高危确认只能在桌面评审 skill / Story 详情页 UI 以完整上下文"
            f"完成(终态挂起门走 UI 的 /lifecycle/ui-upgrade)。"
        ),
        "gate": gate,
    }


def check_confirm_token(
    story_key: str, gate: dict, confirm_token: str
) -> dict | None:
    """对一个可推进的确认门做执法。返回 None = 通过;否则返回拒绝结果 dict。

    规则顺序(§3.2):
    1. 终态类 target(gate.targetIsTerminal)→ 一律拒绝(token 对也不行);
    2. confirm_token != f"{story_key}:{gate.targetState}" → 拒绝并说明正确拼法。
    """
    if gate.get("targetIsTerminal"):
        return _terminal_refusal(story_key, gate)
    if str(confirm_token or "") != build_confirm_token(
        story_key, str(gate.get("targetState") or "")
    ):
        return _token_refusal(story_key, gate)
    return None


# ---- 工具核心(纯逻辑;HTTP 经 fetch/send 注入) ----


def _norm_stories(payload: object) -> list[dict]:
    """/api/story 响应 → list(兼容 list/包一层 dict)。"""
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    if isinstance(payload, dict):
        data = payload.get("stories") or payload.get("data") or []
        if isinstance(data, list):
            return [s for s in data if isinstance(s, dict)]
    return []


def tool_story_list(fetch: FetchFn, status: str = "") -> dict:
    """``story_list(status?)`` — 活跃 story 摘要(key/标题/lifecycle 状态/当前阶段/是否停门)。"""
    path = "/api/story" + (f"?status={urllib.parse.quote(status)}" if status else "")
    stories = _norm_stories(fetch(path))
    rows = [
        {
            "key": s.get("storyKey") or "",
            "title": s.get("title") or "",
            "lifecycleState": s.get("lifecycleState") or "",
            "currentStage": s.get("currentStage") or "",
            "status": s.get("status") or "",
            "atConfirmGate": bool(s.get("awaitingConfirm")),
        }
        for s in stories
    ]
    gated = [r for r in rows if r["atConfirmGate"]]
    lines = [f"- {r['key']} {r['title']} [{r['lifecycleState']}/{r['status']}] 阶段:{r['currentStage']}" for r in rows]
    if gated:
        lines.append("")
        lines.append("停在确认门(可用 story_detail 看 targetState 再 story_advance):")
        lines.extend(f"- {r['key']} (阶段:{r['currentStage']})" for r in gated)
    summary = (
        f"共 {len(rows)} 个 story,其中 {len(gated)} 个停在确认门。"
        + ("" if rows else "列表为空 —— 确认 serve 正常且已建 story。")
    )
    return {"ok": True, "summary": summary, "count": len(rows), "stories": rows, "detail": "\n".join(lines)}


def tool_story_detail(fetch: FetchFn, key: str) -> dict:
    """``story_detail(key)`` — 单 story 全量:状态/stage 进度/judge 摘要/确认门/建议/证据指针。

    确认门详情**必须含 targetState**(LLM 拼 confirm_token 的材料)与
    targetIsTerminal(终态判定由 serve 侧给,butler 不硬编码终态集合)。
    """
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "summary": "缺少 story key,请传入如 tapd-1144381896001067713。"}
    story = _fetch_story_or_none(fetch, key)
    if story is None:
        return {"ok": False, "summary": f"查无 story {key},请用 story_list 核对 key。"}
    if not story:
        return {"ok": False, "summary": f"story {key} 响应异常。"}

    plan = _best_effort(fetch, f"/api/story/{urllib.parse.quote(key)}/plan", {}) or {}
    timeline = _best_effort(fetch, f"/api/story/{urllib.parse.quote(key)}/timeline", {}) or {}
    deliverables = _best_effort(fetch, f"/api/story/{urllib.parse.quote(key)}/deliverables", {}) or {}
    docs = _best_effort(fetch, f"/api/story/{urllib.parse.quote(key)}/docs", {}) or {}
    sessions = _best_effort(fetch, f"/api/story/{urllib.parse.quote(key)}/sessions", {}) or {}

    gates = [g for g in (story.get("confirmGates") or []) if isinstance(g, dict)]
    for g in gates:
        g["confirmTokenFormat"] = build_confirm_token(key, str(g.get("targetState") or ""))

    # stage 进度(plan 的 stages 视图:name/focus/adapter/done)
    stage_progress = [
        {
            "stage": st.get("name") or "",
            "focus": st.get("focus") or "",
            "adapter": st.get("adapter") or "",
            "done": bool(st.get("done")),
        }
        for st in (plan.get("stages") or []) if isinstance(st, dict)
    ]

    # judge 决策摘要(最近 N 条:decision + 人话理由 + 阶段)
    decisions_in = [d for d in (timeline.get("decisions") or []) if isinstance(d, dict)]
    judge_summary = [
        {
            "stage": d.get("stage") or "",
            "decision": d.get("decision") or d.get("verdict") or "",
            "message": (d.get("human_message") or "")[:300],
            "at": d.get("created_at") or "",
        }
        for d in decisions_in[-_RECENT_DECISIONS:]
    ]

    # 证据文件指针:成果物状态(存在/已确认/证据引用)+ story docs 清单 + workspace
    deliverable_view = []
    for d in (deliverables.get("deliverables") or []):
        if not isinstance(d, dict):
            continue
        deliverable_view.append(
            {
                "key": d.get("key"),
                "label": d.get("label"),
                "exists": bool(d.get("exists")),
                "confirmed": bool(d.get("confirmed")),
                "satisfied": bool(d.get("satisfied")),
                "evidence": d.get("evidence") or [],
            }
        )
    evidence_pointers = {
        "workspace": story.get("workspace") or "",
        "deliverables": deliverable_view,
        "nextGate": deliverables.get("gate") or None,
        "docs": [
            {"docType": d.get("doc_type") or d.get("docType") or "", "version": d.get("version")}
            for d in (docs.get("docs") or []) if isinstance(d, dict)
        ],
        "sessions": [
            {
                "stage": x.get("stage") or "",
                "adapter": x.get("adapter") or "",
                "sessionId": x.get("session_id") or "",
                "status": x.get("status") or "",
            }
            for x in (sessions.get("sessions") or []) if isinstance(x, dict)
        ],
    }

    suggestion = _next_step_suggestion(story, gates)

    summary_lines = [
        f"{story.get('storyKey') or key} {story.get('title') or ''} — "
        f"lifecycle={story.get('lifecycleState')}/{story.get('status')},阶段:{story.get('currentStage')}",
    ]
    if gates:
        for g in gates:
            kind = g.get("kind")
            if kind == "upgrade" or g.get("targetIsTerminal"):
                summary_lines.append(
                    f"确认门:{g.get('targetState')}(终态/发布类)—— MCP 拒绝,请走桌面评审/UI。"
                )
            else:
                summary_lines.append(
                    f"确认门:待确认 → {g.get('targetState')},confirm_token = \"{g['confirmTokenFormat']}\"。"
                )
    else:
        summary_lines.append("当前无挂起确认门。")
    summary_lines.append(f"下一步:{suggestion}")

    return {
        "ok": True,
        "summary": "\n".join(summary_lines),
        "storyKey": story.get("storyKey") or key,
        "title": story.get("title") or "",
        "status": story.get("status") or "",
        "lifecycleState": story.get("lifecycleState") or "",
        "currentStage": story.get("currentStage") or "",
        "lastError": story.get("lastError") or "",
        "planConfirmed": bool(story.get("planConfirmed")),
        "hasPlan": bool(story.get("hasPlan")),
        "confirmGates": gates,
        "stageProgress": stage_progress,
        "judgeSummary": judge_summary,
        "nextStep": suggestion,
        "evidence": evidence_pointers,
    }


def _next_step_suggestion(story: dict, gates: list[dict]) -> str:
    """从状态 + 确认门推导下一步建议(纯读推导,无副作用)。"""
    kinds = {g.get("kind") for g in gates}
    if "upgrade" in kinds:
        return "终态/发布类挂起门(上线/结项)只能桌面评审 skill / Story 详情页 UI 确认。"
    for g in gates:
        if g.get("kind") == "story_state" and not g.get("targetIsTerminal"):
            return (
                f"story_advance(key, confirm_token=\"{g.get('confirmTokenFormat')}\")"
                f"推进到「{g.get('targetState')}」。"
            )
    if "stage" in kinds:
        g = next(g for g in gates if g.get("kind") == "stage")
        return (
            f"story_advance(key, confirm_token=\"{g.get('confirmTokenFormat')}\")"
            f"进入下一阶段「{g.get('targetState')}」。"
        )
    if "plan_confirm" in kinds:
        g = next(g for g in gates if g.get("kind") == "plan_confirm")
        return f"plan_confirm(key, confirm_token=\"{g.get('confirmTokenFormat')}\")确认规划并启动执行。"
    status = story.get("status") or ""
    if status == "failed":
        return "story 处于 failed —— POST /api/story/{key}/plan/regenerate 是复位重跑的唯一合法通道。"
    if status == "completed":
        return "story 已完成,无需推进。"
    if status == "paused":
        return "story 暂停且无结构化确认门 —— 建议人工查看 lastError 或终端输出后 PUT /advance 恢复。"
    return "编排线程推进中,稍后再查;或用 patrol_summary 看全局。"


def tool_patrol_summary(
    fetch: FetchFn,
    *,
    detail_limit: int = _PATROL_DETAIL_LIMIT,
    fail_detail_limit: int = _PATROL_FAIL_DETAIL_LIMIT,
    reject_budget: int = DEFAULT_REJECT_BUDGET,
) -> dict:
    """``patrol_summary()`` — 生产巡检发现 + stuck 状态 + 各 story reject 预算余量。

    数据源全为现有端点:列表徽标 patrolSummary(GET /api/story,6f371215 五端点
    之上列表已带批量聚合)、逐 story 巡检轮次(GET /patrol/runs)、逐 story 编排
    决策(GET /timeline,已并入 orchestrator_decision 表)。stuck 信号 = 决策里
    trigger/reason 含 stuck 的升级记录;reject 余量 = 默认预算(3)− 该 stage
    reject 次数。逐 story 拉取有界(detail_limit),超出部分截断说明。
    """
    stories = _norm_stories(fetch("/api/story"))

    # ---- 巡检发现:聚合各 story 的 patrolSummary 徽标;FAIL 的拉最新一轮明细 ----
    patrolled = [
        s for s in stories
        if isinstance(s.get("patrolSummary"), dict)
    ]
    fail_stories = [
        s for s in patrolled if (s["patrolSummary"].get("latestResult") or "").upper() == "FAIL"
    ]
    patrol_findings = []
    for s in fail_stories[:fail_detail_limit]:
        runs = _best_effort(
            fetch, f"/api/story/{urllib.parse.quote(s['storyKey'])}/patrol/runs?limit=1", {}
        ) or {}
        run_list = runs.get("runs") or []
        latest = run_list[0] if run_list else {}
        patrol_findings.append(
            {
                "storyKey": s.get("storyKey"),
                "latestRunAt": s["patrolSummary"].get("latestRunAt"),
                "summary": latest.get("summary") or "",
                "failItems": [
                    {"name": fi.get("name"), "observed": fi.get("observed")}
                    for fi in (latest.get("items") or [])
                    if isinstance(fi, dict) and (fi.get("result") or "").upper() == "FAIL"
                ],
            }
        )
    never = [
        s.get("storyKey") for s in stories if not isinstance(s.get("patrolSummary"), dict)
    ]

    # ---- 逐 story:stuck 状态 + reject 预算余量(只看 active/paused,有界) ----
    busy = [s for s in stories if (s.get("status") or "") in ("active", "paused")]
    truncated = max(0, len(busy) - detail_limit)
    per_story = []
    stuck_total = 0
    for s in busy[:detail_limit]:
        timeline = _best_effort(
            fetch, f"/api/story/{urllib.parse.quote(s['storyKey'])}/timeline", {}
        ) or {}
        decisions = [d for d in (timeline.get("decisions") or []) if isinstance(d, dict)]
        stuck_hits = [
            d for d in decisions
            if "stuck" in str(d.get("reason_code") or "").lower()
            or "stuck" in str(d.get("human_message") or "").lower()
        ]
        stuck_total += 1 if stuck_hits else 0
        rejects: dict[str, int] = {}
        for d in decisions:
            if (d.get("decision") or "") == "reject":
                stage = d.get("stage") or ""
                rejects[stage] = rejects.get(stage, 0) + 1
        budget_rows = [
            {"stage": st, "rejected": n, "remaining": max(0, reject_budget - n)}
            for st, n in sorted(rejects.items())
        ]
        per_story.append(
            {
                "storyKey": s.get("storyKey"),
                "status": s.get("status") or "",
                "currentStage": s.get("currentStage") or "",
                "atConfirmGate": bool(s.get("awaitingConfirm")),
                "stuck": bool(stuck_hits),
                "stuckHint": (stuck_hits[-1].get("human_message") or "")[:200] if stuck_hits else "",
                "rejectBudget": budget_rows,
            }
        )

    summary_lines = [
        f"巡检:登记 {len(patrolled)} 个 story,{len(fail_stories)} 个最新一轮 FAIL,"
        f"{len(never)} 个从未巡检。",
        f"卡住:被巡的 {min(len(busy), detail_limit)} 个活跃 story 中 {stuck_total} 个有 stuck 升级记录。",
        f"reject 预算:默认上限 {reject_budget}/stage(服务端 STORY_REJECT_LIMIT 可改),明细见 perStory。",
    ]
    if truncated:
        summary_lines.append(f"(活跃 story 超过 {detail_limit} 个,只逐个巡了前 {detail_limit} 个。)")
    return {
        "ok": True,
        "summary": "\n".join(summary_lines),
        "patrol": {
            "patrolledCount": len(patrolled),
            "failCount": len(fail_stories),
            "neverPatrolled": len(never),
            "failFindings": patrol_findings,
        },
        "stuckCount": stuck_total,
        "perStory": per_story,
        "rejectBudgetDefault": reject_budget,
    }


def _pick_advance_gate(gates: list[dict]) -> dict | None:
    """从确认门列表里挑 story_advance 可推进的那一个(story_state 优先,其次 stage)。"""
    for kind in ("story_state", "stage"):
        for g in gates:
            if g.get("kind") == kind:
                return g
    return None


def tool_story_advance(
    fetch: FetchFn,
    send: SendFn,
    key: str,
    confirm_token: str,
    confirmed_via: str = "desktop",
) -> dict:
    """``story_advance(key, confirm_token, confirmed_via="desktop")`` — 执法后推进确认门。

    执法(§3.2):token 精确等于 f"{key}:{targetState}";终态类 target 一律拒绝。
    通过后按门类型 POST/PUT 到对应 advance 端点并透传 confirmed_via(账本,§4):
    - story_state 门 → POST /api/story/{key}/lifecycle/advance
    - stage 门      → PUT /api/story/{key}/advance
    """
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "summary": "缺少 story key。"}
    if confirmed_via not in VALID_CONFIRMED_VIA:
        return {
            "ok": False,
            "summary": (
                f"confirmed_via 只允许 {'/'.join(VALID_CONFIRMED_VIA)}(账本字段,防脏审计),"
                f"收到:{confirmed_via!r}。"
            ),
        }

    story = _fetch_story_or_none(fetch, key)
    if story is None:
        return {"ok": False, "summary": f"查无 story {key},请用 story_list 核对 key。"}
    if not story:
        return {"ok": False, "summary": f"story {key} 响应异常。"}
    gates = [g for g in (story.get("confirmGates") or []) if isinstance(g, dict)]

    gate = _pick_advance_gate(gates)
    if gate is None:
        if any(g.get("kind") == "upgrade" for g in gates):
            return _terminal_refusal(key, next(g for g in gates if g.get("kind") == "upgrade"))
        if any(g.get("kind") == "plan_confirm" for g in gates):
            return {
                "ok": False,
                "refused": "no_advance_gate",
                "summary": "该 story 挂着的是「规划确认门」,请改用 plan_confirm 工具。",
            }
        return {
            "ok": False,
            "refused": "no_gate",
            "summary": (
                f"story {key} 当前没有待确认的确认门(状态 {story.get('status')},"
                f"阶段 {story.get('currentStage')})—— 无需推进;详情用 story_detail。"
            ),
        }

    refusal = check_confirm_token(key, gate, confirm_token)
    if refusal is not None:
        return refusal

    # 执法通过 → 透传 confirmed_via 落账本
    if gate.get("kind") == "story_state":
        path = f"/api/story/{urllib.parse.quote(key)}/lifecycle/advance"
        method = "POST"
    else:  # stage 门:PUT /advance(paused 分支清 _stage_gate 续跑)
        path = f"/api/story/{urllib.parse.quote(key)}/advance"
        method = "PUT"
    status, parsed = send(method, path, {"confirmed_via": confirmed_via})
    if status >= 400:
        return {
            "ok": False,
            "refused": "backend_rejected",
            "summary": f"serve 拒绝了这次推进(HTTP {status}):{_http_error_text(status, parsed)}",
        }
    return {
        "ok": True,
        "summary": (
            f"已确认推进 {key} → 「{gate.get('targetState')}」(confirmed_via={confirmed_via},"
            f"已落账本)。"
        ),
        "gate": gate,
        "confirmedVia": confirmed_via,
        "response": parsed if isinstance(parsed, dict) else {"raw": parsed},
    }


def tool_plan_confirm(
    fetch: FetchFn,
    send: SendFn,
    key: str,
    confirm_token: str,
) -> dict:
    """``plan_confirm(key, confirm_token)`` — 同款执法后确认规划(POST /plan/confirm)。

    规划确认门:有规划(_agent_actions)且未确认(_plan_confirmed)时挂起,
    targetState = 「开发」(确认后 lifecycle 推进到开发态,serve 侧语义)。
    """
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "summary": "缺少 story key。"}
    story = _fetch_story_or_none(fetch, key)
    if story is None:
        return {"ok": False, "summary": f"查无 story {key},请用 story_list 核对 key。"}
    if not story:
        return {"ok": False, "summary": f"story {key} 响应异常。"}
    gates = [g for g in (story.get("confirmGates") or []) if isinstance(g, dict)]
    gate = next((g for g in gates if g.get("kind") == "plan_confirm"), None)
    if gate is None:
        if story.get("planConfirmed"):
            return {"ok": False, "refused": "already_confirmed", "summary": "该 story 的规划已确认过,无需重复确认。"}
        if not story.get("hasPlan"):
            return {
                "ok": False,
                "refused": "no_plan",
                "summary": "该 story 还没有规划 —— 先走规划端点(/plan/stream 或 /plan/regenerate)生成 _agent_actions。",
            }
        return {"ok": False, "refused": "no_gate", "summary": "该 story 当前没有挂起的规划确认门。"}

    refusal = check_confirm_token(key, gate, confirm_token)
    if refusal is not None:
        return refusal

    status, parsed = send(
        "POST", f"/api/story/{urllib.parse.quote(key)}/plan/confirm", {}
    )
    if status >= 400:
        return {
            "ok": False,
            "refused": "backend_rejected",
            "summary": f"serve 拒绝了规划确认(HTTP {status}):{_http_error_text(status, parsed)}",
        }
    return {
        "ok": True,
        "summary": f"规划已确认,{key} 进入「{gate.get('targetState')}」,编排线程将接手执行。",
        "confirmedVia": "desktop",
        "response": parsed if isinstance(parsed, dict) else {"raw": parsed},
    }


def tool_session_register(
    send: SendFn,
    key: str,
    stage: str,
    adapter: str,
    session_id: str,
) -> dict:
    """``session_register(key, stage, adapter, session_id)`` — sessions 簿记。

    把交互会话(如 zcode 的 sess id)登记到 story+stage(story_session 表)——
    补「zcode 不是 adapter」的断点续跑缺口:前端「复制 resume 文案」与后续
    排查都从 DB 读这次登记。stage/adapter 缺省由 serve 端兜底(当前 stage)。
    """
    key = str(key or "").strip()
    session_id = str(session_id or "").strip()
    if not key or not session_id:
        return {"ok": False, "summary": "key 与 session_id 都必填(zcode 会话传它的 sess id)。"}
    status, parsed = send(
        "POST",
        f"/api/story/{urllib.parse.quote(key)}/session",
        {
            "session_id": session_id,
            "stage": str(stage or "").strip(),
            "adapter": str(adapter or "").strip(),
        },
    )
    if status == 404:
        return {"ok": False, "refused": "not_found", "summary": f"查无 story {key},请核对 key。"}
    if status >= 400:
        return {
            "ok": False,
            "refused": "backend_rejected",
            "summary": f"serve 拒绝了会话登记(HTTP {status}):{_http_error_text(status, parsed)}",
        }
    return {
        "ok": True,
        "summary": f"会话已登记:{key} / {stage or '(当前阶段)'} / {adapter or '(默认 adapter)'} → {session_id}。",
        "response": parsed if isinstance(parsed, dict) else {"raw": parsed},
    }


def tool_knowledge_search(
    fetch: FetchFn,
    q: str,
    story_key: str = "",
    stage: str = "",
    top_k: int = 5,
) -> dict:
    """``knowledge_search(q, story_key?, top_k?)`` — 知识库检索(只读,第 7 工具)。

    薄包 serve 的 ``GET /api/knowledge/search``(WP-F §8.2;serve 侧降级不 500,
    连接层失败由 dispatch 统一转「后端未启动」友好文案)。结果带 source_refs
    (知识出处),给 LLM 复用结论时溯源。
    """
    q = str(q or "").strip()
    if not q:
        return {
            "ok": False,
            "summary": "缺少检索词 q —— 例如 knowledge_search(q=\"联系人 落表\")。",
        }
    try:
        top_k = max(1, min(int(top_k or 5), 50))
    except (TypeError, ValueError):
        top_k = 5
    params = {"q": q, "top_k": str(top_k)}
    if str(story_key or "").strip():
        params["story_key"] = str(story_key).strip()
    if str(stage or "").strip():
        params["stage"] = str(stage).strip()
    payload = fetch(f"/api/knowledge/search?{urllib.parse.urlencode(params)}")
    if not isinstance(payload, dict):
        payload = {}
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    rows = [
        {
            "title": r.get("title") or "",
            "type": r.get("type") or "",
            "category": r.get("category") or "",
            "detail": (r.get("detail") or r.get("summary") or "")[:300],
            "tags": r.get("tags") or [],
            "source_refs": r.get("source_refs") or [],
        }
        for r in results
    ]
    lines = []
    for r in rows:
        src = r["source_refs"][0] if r["source_refs"] else ""
        lines.append(
            f"- {r['title']}({r['category'] or r['type']}) {r['detail']}"
            + (f" 来源:{src}" if src else "")
        )
    warning = str(payload.get("warning") or "")
    summary_lines = [f"知识检索「{q}」命中 {len(rows)} 条。"] if rows else [f"知识检索「{q}」无命中。"]
    if warning:
        summary_lines.append(f"(降级提示:{warning})")
    summary_lines.extend(lines)
    out = {
        "ok": True,
        "summary": "\n".join(summary_lines),
        "query": q,
        "count": len(rows),
        "results": rows,
    }
    if warning:
        out["warning"] = warning
    return out


# ---- 工具 schema(tools/list 暴露;inputSchema 照 clarify 的手写 JSON Schema) ----


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


BUTLER_TOOLS = [
    _tool(
        "story_list",
        "列出活跃 story 的摘要(key/标题/lifecycle 状态/当前阶段/是否停在确认门)。"
        "先看全局再挑story,停门 story 用 story_detail 拿 targetState。",
        {
            "status": {
                "type": "string",
                "description": "可选,按引擎状态过滤(active/paused/completed/failed),缺省全部可见 story。",
            },
        },
        [],
    ),
    _tool(
        "story_detail",
        "单个 story 全量:状态、stage 进度、judge 决策摘要、确认门详情(含 targetState"
        "—— 拼 confirm_token 用)、下一步建议、证据文件指针。推进前必读。",
        {"key": {"type": "string", "description": "story key,如 tapd-1144381896001067713。"}},
        ["key"],
    ),
    _tool(
        "patrol_summary",
        "生产巡检发现(FAIL 明细/从未巡检)+ 各活跃 story 的 stuck 状态 + reject 预算余量"
        "(默认上限 3/stage)。排查/晨会态势用。",
        {},
        [],
    ),
    _tool(
        "story_advance",
        "推进一个确认门(服务端执法)。confirm_token 必须精确等于"
        ' f"{story_key}:{target_state}"(target_state 以 story_detail 返回的为准);'
        "终态/发布类 target(上线/结项)一律拒绝,请走桌面评审 skill / UI。",
        {
            "key": {"type": "string", "description": "story key。"},
            "confirm_token": {
                "type": "string",
                "description": '执法凭据,精确等于 "story_key:target_state"。',
            },
            "confirmed_via": {
                "type": "string",
                "description": "确认来源,默认 desktop;可选 desktop/wechat/ui/api(进审计账本)。",
            },
        },
        ["key", "confirm_token"],
    ),
    _tool(
        "plan_confirm",
        "确认规划并启动执行(同款 token 执法;有规划未确认时才可用)。确认后 story 进入"
        "「开发」,编排线程接手。",
        {
            "key": {"type": "string", "description": "story key。"},
            "confirm_token": {
                "type": "string",
                "description": '执法凭据,精确等于 "story_key:target_state"(target 见 story_detail 的 plan_confirm 门)。',
            },
        },
        ["key", "confirm_token"],
    ),
    _tool(
        "session_register",
        "把一个交互会话(如 zcode 的 sess id)登记到 story+stage,断点续跑有据可查。"
        "stage/adapter 留空则用 story 当前值。",
        {
            "key": {"type": "string", "description": "story key。"},
            "stage": {"type": "string", "description": "阶段名;留空取 story 当前阶段。"},
            "adapter": {"type": "string", "description": "会话类型标识,如 zcode;留空默认 claude。"},
            "session_id": {"type": "string", "description": "会话 id,如 sess_xxx。"},
        },
        ["key", "session_id"],
    ),
    _tool(
        "knowledge_search",
        "检索团队知识库(场景/打法/踩坑/RUN 跑测新坑/wiki)。只读。排查问题前先查,"
        "命中可直接复用结论;条目带 source_refs 指向原始出处。",
        {
            "q": {"type": "string", "description": "检索关键词,如 联系人 落表 / occupationType。"},
            "story_key": {"type": "string", "description": "可选,按 story key 召回相关条目。"},
            "top_k": {"type": "integer", "description": "返回条数,默认 5。"},
        },
        ["q"],
    ),
]

_TOOL_NAMES = [t["name"] for t in BUTLER_TOOLS]


# ---- stdio JSONRPC loop(薄 I/O 层,照 clarify_server.run_server) ----


def dispatch_tool(name: str, args: dict) -> dict:
    """分发一次工具调用 → 统一 {ok, summary, ...} 信封;异常转友好错误。

    模块级(非 run_server 内嵌)便于单测:serve 不在时经真实 HTTP 层也能测到
    友好错误路径(monkeypatch urllib.request.urlopen 抛 URLError)。
    """
    try:
        if name == "story_list":
            return tool_story_list(make_fetch(), status=str(args.get("status", "") or ""))
        if name == "story_detail":
            return tool_story_detail(make_fetch(), key=str(args.get("key", "") or ""))
        if name == "patrol_summary":
            return tool_patrol_summary(make_fetch())
        if name == "story_advance":
            return tool_story_advance(
                make_fetch(),
                make_send(),
                key=str(args.get("key", "") or ""),
                confirm_token=str(args.get("confirm_token", "") or ""),
                confirmed_via=str(args.get("confirmed_via", "") or "desktop"),
            )
        if name == "plan_confirm":
            return tool_plan_confirm(
                make_fetch(),
                make_send(),
                key=str(args.get("key", "") or ""),
                confirm_token=str(args.get("confirm_token", "") or ""),
            )
        if name == "session_register":
            return tool_session_register(
                make_send(),
                key=str(args.get("key", "") or ""),
                stage=str(args.get("stage", "") or ""),
                adapter=str(args.get("adapter", "") or ""),
                session_id=str(args.get("session_id", "") or ""),
            )
        if name == "knowledge_search":
            return tool_knowledge_search(
                make_fetch(),
                q=str(args.get("q", "") or ""),
                story_key=str(args.get("story_key", "") or ""),
                stage=str(args.get("stage", "") or ""),
                top_k=args.get("top_k", 5),
            )
        return {
            "ok": False,
            "summary": f"未知工具:{name}。可用:{'、'.join(_TOOL_NAMES)}。",
        }
    except ButlerHttpError as e:
        # 后端在,但这笔请求被拒(如 409 成果物 gate 未满足)—— 原样转述,别误报"未启动"。
        return {"ok": False, "summary": str(e)}
    except ButlerServeError:
        return backend_down_result()
    except Exception as e:  # noqa: BLE001 — 工具面绝不炸宿主会话
        return {"ok": False, "summary": f"管家工具执行失败:{e}"}


def _mcp_result(payload: dict) -> dict:
    """{ok, summary, ...} → MCP result(content text = 摘要 + 结构化 JSON)。"""
    text = str(payload.get("summary", "")) + "\n" + json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def run_server() -> None:
    """stdio MCP server 主循环(JSONRPC over stdin/stdout)。

    握手 initialize → notifications/initialized → tools/list → tools/call。
    每次工具调用现场构造 fetch/send(读 env STORY_SERVE_URL);任何异常都转成
    友好文本结果(绝不抛出炸宿主会话,§5 降级矩阵)。
    """
    # Windows Python 默认 stdout 编码非 UTF-8,写中文会 UnicodeEncodeError(同 clarify)。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    def _send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "butler", "version": "1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            pass  # 通知,无需响应
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": BUTLER_TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            result = dispatch_tool(
                str(params.get("name") or ""), dict(params.get("arguments") or {})
            )
            _send({"jsonrpc": "2.0", "id": mid, "result": _mcp_result(result)})
        # 其他 method(spec 的 ping 等)忽略 —— 与 clarify 同款最小实现。


# ---- zcode 侧配置注入(write_mcp_config 风格,merge 不覆盖) ----


def write_butler_mcp_config(config_path, python_bin: str, serve_url: str | None = None) -> str:
    """把 ``butler`` MCP server 写进配置文件的 ``mcpServers``(**merge 语义**)。

    仿 clarify 的 ``write_mcp_config``,但**不覆盖既有条目**:读已有 JSON(存在
    的话),只加/改 ``butler`` 键 —— clarify 写的 ``lifecycle`` 等其他 server 原样
    保留。``serve_url`` 传了才写 ``env.STORY_SERVE_URL``(默认不写,server 端用
    127.0.0.1:8180)。返回配置文件路径。

    手动等价 JSON 见模块 docstring。clarify 没有 CLI 接线,本函数同样只提供
    函数级入口(不挂 CLI 命令)。
    """
    from pathlib import Path as _Path

    p = _Path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    if p.exists():
        try:
            config = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            config = {}
    if not isinstance(config, dict):
        config = {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    entry: dict = {
        "command": python_bin,
        "args": ["-m", "story_lifecycle.orchestrator.mcp.butler_server"],
    }
    if serve_url:
        entry["env"] = {"STORY_SERVE_URL": serve_url}
    servers["butler"] = entry
    config["mcpServers"] = servers
    p.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


if __name__ == "__main__":
    run_server()
