"""Brief C：给 TAPD story 打 task_type 标签（受控 12 类）。

用法：
  cd packages/story-miner && PYTHONIOENCODING=utf-8 python scripts/classify_story_task_type.py

输入：scripts/out/bug_story_graph.json（默认）
输出：scripts/out/story_task_types.json
可选：--write-db 把 task_type 写回 ~/.story-lifecycle/story.db 的 context_json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import yaml
from openai import OpenAI

_PROJ = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ.parent / "story-lifecycle" / "src"))

TYPES = [
    "credit-limit",      # 授信/额度/风控
    "fund-flow",         # 放款/还款/提现/清分/对账
    "message-notify",    # 短信/OTP/通知/模板
    "marketing",         # 营销/活动/MGM/券/免息
    "user-profile",      # 用户/资料/认证/隐私
    "order",             # 订单/交易
    "integration",       # 三方对接/回调
    "gateway-infra",     # 网关/限流/配置/调度/状态机
    "data-sql",          # SQL/查询/迁移
    "frontend",          # 前端/admin/页面
    "deploy",            # 部署/上线/发版
    "debug",             # 排查/定位
]

TYPE_DESC = {
    "credit-limit": "授信/额度/风控/反欺诈",
    "fund-flow": "放款/还款/提现/清分/对账/溢缴款/退款",
    "message-notify": "短信/OTP/通知/模板/站内信/推送",
    "marketing": "营销/活动/MGM/券/免息/人群圈选",
    "user-profile": "用户/资料/认证/隐私/职业/联系人",
    "order": "订单/交易/支付",
    "integration": "三方对接/回调/外部系统",
    "gateway-infra": "网关/限流/配置/调度/状态机/公共基础设施",
    "data-sql": "SQL/查询/报表/数据迁移",
    "frontend": "前端/admin/页面/展示",
    "deploy": "部署/上线/发版/环境",
    "debug": "排查/定位/问题复现/日志",
}


def load_config():
    home = Path.home()
    cfg_path = home / ".story-lifecycle" / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_client(cfg: dict) -> OpenAI:
    return OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
    )


SYSTEM_PROMPT = """你是一个严格的故事分类助手。对每条输入的故事，必须从给定的 12 个类别中选择一个最贴切的 task_type。

类别列表（key: 含义）：
""" + "\n".join(f"- {k}: {TYPE_DESC[k]}" for k in TYPES) + """

规则：
1. 只返回给定 12 个 key 之一，不要发明新类别。
2. 若一个故事横跨多类，选择其核心业务目标最接近的类。
3. 优先用标题中的业务关键词判断，不需要额外解释。
4. 输出必须是 JSON 数组，每个元素：{"story_key": "...", "task_type": "..."}
"""


def classify_batch(client: OpenAI, model: str, items: list[dict]) -> list[dict]:
    """调用 LLM 对一批 story 分类。items: [{story_key, title}]"""
    user_text = "请对以下故事进行分类（只返回 JSON 数组）：\n\n" + "\n".join(
        f"{i+1}. story_key={it['story_key']} | title={it['title']}"
        for i, it in enumerate(items)
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # 有时模型会包一层 {"results": [...]}
        try:
            # 尝试找数组
            start = content.find("[")
            end = content.rfind("]")
            if start != -1 and end != -1:
                parsed = json.loads(content[start:end + 1])
            else:
                raise
        except Exception:
            raise RuntimeError(f"LLM 返回无法解析的 JSON: {content[:500]}")
    if isinstance(parsed, dict):
        # 找第一个数组值
        for v in parsed.values():
            if isinstance(v, list):
                parsed = v
                break
    if not isinstance(parsed, list):
        raise RuntimeError(f"LLM 未返回数组: {content[:500]}")
    return parsed


def normalize_type(raw: str) -> str:
    """把模型输出映射到受控 12 类。"""
    key = raw.strip().lower()
    # 直接匹配
    if key in TYPES:
        return key
    # 常见别名/错误
    aliases = {
        "credit": "credit-limit",
        "limit": "credit-limit",
        "risk": "credit-limit",
        "风控": "credit-limit",
        "额度": "credit-limit",
        "授信": "credit-limit",
        "fund": "fund-flow",
        "放款": "fund-flow",
        "还款": "fund-flow",
        "提现": "fund-flow",
        "清分": "fund-flow",
        "对账": "fund-flow",
        "message": "message-notify",
        "notify": "message-notify",
        "短信": "message-notify",
        "通知": "message-notify",
        "otp": "message-notify",
        "market": "marketing",
        "营销": "marketing",
        "活动": "marketing",
        "mgm": "marketing",
        "券": "marketing",
        "免息": "marketing",
        "user": "user-profile",
        "用户": "user-profile",
        "资料": "user-profile",
        "认证": "user-profile",
        "order": "order",
        "订单": "order",
        "交易": "order",
        "integration": "integration",
        "三方": "integration",
        "回调": "integration",
        "gateway": "gateway-infra",
        "infra": "gateway-infra",
        "网关": "gateway-infra",
        "限流": "gateway-infra",
        "配置": "gateway-infra",
        "调度": "gateway-infra",
        "状态机": "gateway-infra",
        "sql": "data-sql",
        "数据": "data-sql",
        "查询": "data-sql",
        "迁移": "data-sql",
        "frontend": "frontend",
        "前端": "frontend",
        "admin": "frontend",
        "页面": "frontend",
        "deploy": "deploy",
        "部署": "deploy",
        "上线": "deploy",
        "发版": "deploy",
        "debug": "debug",
        "排查": "debug",
        "定位": "debug",
    }
    if key in aliases:
        return aliases[key]
    # 模糊匹配：看哪个类型描述包含 raw
    for k, desc in TYPE_DESC.items():
        if key in k or key in desc:
            return k
    # 兜底：unknown（不是 debug），让无法归类的可见、不静默塌缩
    return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in-graph", default=str(_PROJ / "scripts" / "out" / "bug_story_graph.json"))
    ap.add_argument("--out", default=str(_PROJ / "scripts" / "out" / "story_task_types.json"))
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--write-db", action="store_true", help="写回 story.db 的 context_json")
    ap.add_argument("--sleep", type=float, default=0.5, help="批次间休眠秒数")
    args = ap.parse_args()

    cfg = load_config()
    client = build_client(cfg)
    model = cfg.get("model", "deepseek-v4-pro")

    with open(args.in_graph, "r", encoding="utf-8") as f:
        graph = json.load(f)

    stories = graph.get("stories", [])
    items = [{"story_key": s["story_key"], "title": s["title"]} for s in stories]
    print(f"loaded {len(items)} stories from {args.in_graph}")

    results: list[dict] = []
    for start in range(0, len(items), args.batch_size):
        batch = items[start:start + args.batch_size]
        print(f"  classifying batch {start//args.batch_size + 1}/{(len(items)-1)//args.batch_size + 1} ({len(batch)} items) ...")
        try:
            raw_results = classify_batch(client, model, batch)
        except Exception as e:
            # 重试一次（refresh 时 API 偶发失败）
            try:
                time.sleep(args.sleep * 2)
                raw_results = classify_batch(client, model, batch)
            except Exception as e2:
                print(f"  ERROR batch failed (重试后仍失败): {e2}")
                # 标 unknown（不是 debug），让失败可见、不静默塌缩成单一类型
                raw_results = [{"story_key": it["story_key"], "task_type": "unknown"} for it in batch]
        for r in raw_results:
            results.append({
                "story_key": r.get("story_key", ""),
                "title": next((it["title"] for it in items if it["story_key"] == r.get("story_key", "")), ""),
                "task_type": normalize_type(r.get("task_type", "debug")),
            })
        time.sleep(args.sleep)

    # 确保数量一致
    missing = {it["story_key"] for it in items} - {r["story_key"] for r in results}
    for key in missing:
        title = next(it["title"] for it in items if it["story_key"] == key)
        results.append({"story_key": key, "title": title, "task_type": "unknown"})

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.out} ({len(results)} items)")

    # 分布统计
    dist = Counter(r["task_type"] for r in results)
    print("\n=== task_type 分布 ===")
    for t in TYPES:
        print(f"  {t}: {dist.get(t, 0)}")
    empty = [t for t in TYPES if dist.get(t, 0) == 0]
    if empty:
        print(f"\n  0 个的类（盲区）: {empty}")
    else:
        print("\n  无盲区，12 类均有命中。")

    # 与 bug 图谱聚合
    bug_map = {s["story_key"]: s["bug_count"] for s in stories}
    type_bugs = Counter()
    type_counts = Counter()
    for r in results:
        tt = r["task_type"]
        type_counts[tt] += 1
        type_bugs[tt] += bug_map.get(r["story_key"], 0)
    print("\n=== 按 task_type 聚合 bug 数 ===")
    for t in TYPES:
        n = type_counts.get(t, 0)
        b = type_bugs.get(t, 0)
        avg = round(b / n, 2) if n else 0
        print(f"  {t}: stories={n}, bugs={b}, avg={avg}")

    # 抽检输出
    print("\n=== 抽检 5 条 ===")
    for r in results[:: max(1, len(results) // 5)][:5]:
        print(f"  [{r['task_type']}] {r['story_key']}: {r['title'][:60]}")

    # 写回 story.db
    if args.write_db:
        db_path = Path.home() / ".story-lifecycle" / "story.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        updated = 0
        for r in results:
            cur.execute("SELECT context_json FROM story WHERE story_key=?", (r["story_key"],))
            row = cur.fetchone()
            if not row:
                continue
            ctx = json.loads(row[0] or "{}")
            ctx["task_type"] = r["task_type"]
            cur.execute(
                "UPDATE story SET context_json=? WHERE story_key=?",
                (json.dumps(ctx, ensure_ascii=False), r["story_key"]),
            )
            updated += 1
        conn.commit()
        conn.close()
        print(f"\n  wrote {updated} task_type values back to {db_path}")


if __name__ == "__main__":
    main()
