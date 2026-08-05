"""失败模式挖掘 v3 — 两阶段：逐 case 主题归类 + 主题名归并 ≤10 组。

阶段 1 已跑（52 主题）。本脚本读快照重新逐 case 归类，收集主题名，
再批量归并成 ≤10 个最终主题。走 opencode-go。
"""

import concurrent.futures
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
RESULTS = PACKAGE_ROOT / "results"


class ThemeOut(BaseModel):
    theme: str = Field(description="主题名（4-12 字中文）")
    reason: str = Field(description="一句话归类理由")


class MergeOut(BaseModel):
    groups: dict[str, list[str]] = Field(description="最终主题名 → 待归并的主题名列表")


def _load_cases() -> list[dict]:
    rows = [
        json.loads(l)
        for l in (SNAP / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    return [
        r for r in rows
        if (r.get("conformance_score") or {}).get("alignment") is not None
        and r["conformance_score"]["alignment"] <= 2
    ]


def _prompt(r: dict) -> str:
    cf = r["conformance_score"]
    return (
        "你是失败模式分析师。把案例归入主题（4-12 字中文，覆盖性优先：同类根因必须同名）。\n\n"
        f"repo={r['repo']} merge={r['merge_hash'][:10]} story_key={r.get('story_key','') or '无'}\n"
        f"align={cf.get('alignment')} cov={cf.get('coverage')} drift={cf.get('scope_drift')}\n"
        f"summary: {cf.get('summary','')[:250]}\n"
        f"findings:\n" + "\n".join(f"  - {f[:150]}" for f in (cf.get('findings') or [])[:3]) +
        "\n\n只输出 JSON: {\"theme\": \"主题名\", \"reason\": \"一句话理由\"}"
    )


def _merge_prompt(names: list[str]) -> str:
    """单批主题名归并 prompt（≤15 个，强制简洁 JSON）。"""
    list_txt = "\n".join(f"- {n}" for n in names)
    return (
        "以下是一次代码交付失败模式分析的候选主题名。把它们归并成更少的主题（语义相同合并），"
        "输出『最终主题名 → 该主题包含的候选主题名』映射。\n\n"
        f"{list_txt}\n\n"
        "规则：最终主题 3-8 个；每个候选主题名必须恰好属于一个最终主题；"
        "『需求覆盖不足/覆盖不全/覆盖缺失/覆盖度低』这类同义名必须合并。\n"
        "只输出 JSON 对象: {\"groups\": {\"最终主题名\": [\"候选1\", \"候选2\"], ...}}"
    )


def _merge_names_batch(names: list[str]) -> dict[str, list[str]]:
    """调 LLM 归并一批主题名；失败则该批各自独立成组。"""
    from eval.judges import _LLM

    try:
        res = _LLM.invoke_structured(_merge_prompt(names), MergeOut)
        return res.groups or {}
    except Exception as e:  # noqa: BLE001
        print(f"归并批失败（{len(names)} 名），各自独立: {str(e)[:80]}", file=sys.stderr)
        return {n: [n] for n in names}


def main() -> dict:
    cases = _load_cases()
    from eval.judges import configure_llm_env, _LLM

    configure_llm_env()
    concurrency = int(os.environ.get("EVAL_LLM_CONCURRENCY", "8"))

    # 阶段 1：逐 case 归类
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_LLM.invoke_structured, _prompt(r), ThemeOut) for r in cases]
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            try:
                res = fut.result()
                out.append({"theme": res.theme.strip(), "reason": res.reason.strip()})
            except Exception as e:  # noqa: BLE001
                out.append({"theme": "LLM失败", "reason": str(e)[:80]})
            if i % 20 == 0 or i == len(cases):
                print(f"阶段1 {i}/{len(cases)}", file=sys.stderr)

    theme_counts = Counter(o["theme"] for o in out)

    # 阶段 2：主题名分批归并（每批 ≤15，失败各自独立）
    uniq_names = list(theme_counts.keys())
    groups: dict[str, list[str]] = {}
    for i in range(0, len(uniq_names), 15):
        batch = uniq_names[i : i + 15]
        groups.update(_merge_names_batch(batch))
    # 兜底：未被归并的主题名自成一组
    assigned = {n for names in groups.values() for n in names}
    for n in uniq_names:
        if n not in assigned:
            groups.setdefault(n, []).append(n)

    # 装配 case → 最终主题
    name_to_final = {name: final for final, names in groups.items() for name in names}
    for r, o in zip(cases, out):
        r["_theme"] = name_to_final.get(o["theme"], o["theme"])
        r["_theme_reason"] = o["reason"]

    _render(cases)
    final_counts = Counter(r["_theme"] for r in cases)
    return {"total": len(cases), "stage1_themes": len(theme_counts), "final_themes": len(final_counts)}


def _render(cases: list[dict]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for r in cases:
        by_theme[r["_theme"]].append(r)

    lines = ["# 失败模式挖掘报告 20260805（基于 snapshot_20260805）", ""]
    lines.append(f"- 分析 case 数: {len(cases)}（快照 merge_scores 中 alignment≤2 全部）")
    lines.append(f"- 最终主题数: {len(by_theme)}（两阶段：逐 case 归类 → 主题名归并；端点 opencode-go）")
    lines.append("")

    known = {
        "参照物缺失": ["参照物", "无参照", "link-only", "只有链接"],
        "跨服务需求单仓切片": ["跨服务", "单仓", "切片", "跨仓", "只交付", "部分服务"],
        "先开发后补录需求单": ["先开发", "补录", "时间倒挂", "需求创建"],
        "同域概念混淆错链": ["混淆", "错链", "无关", "另一", "不对应", "其他需求", "错配"],
    }
    lines += ["## 已知模式校验", ""]
    for name, kws in known.items():
        hit = [r for r in cases if any(k in (r["_theme"] + r["_theme_reason"]) for k in kws)]
        lines.append(f"- **{name}**: {len(hit)} 个 case")
    lines.append("")

    lines += ["## 主题清单（按 merge 数排序）", ""]
    for theme, rs in sorted(by_theme.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {theme}（{len(rs)} 个 merge）")
        repos = Counter(r["repo"] for r in rs)
        months = Counter((r.get("merged_at") or "")[:7] for r in rs)
        in_pipe = sum(1 for r in rs if r.get("story_key"))
        lines.append(
            f"- repo: {dict(repos.most_common(5))} | 月份: {dict(sorted(months.items()))} | "
            f"管线内 {in_pipe} / 管线外 {len(rs)-in_pipe}"
        )
        lines.append("- 典型 case 3 条:")
        for r in rs[:3]:
            lines.append(
                f"  - {r['repo']}:{r['merge_hash'][:10]} tapd={r.get('tapd_id','')[:8]} "
                f"align={r['conformance_score']['alignment']} | {r['_theme_reason'][:100]}"
            )
        lines.append("")

    suggestions = [
        (("跨服务", "单仓", "跨仓"), "verify gate 增加 related_services 逐服务交付面核对：spec 声明 N 个服务时，diff 未覆盖的服务显式列『未交付』并进 findings。"),
        (("先开发", "补录", "时间倒挂"), "intake 检测 merge 早于需求创建的时间倒挂 → 显式 warning + 人工确认，防 verify 误判。"),
        (("混淆", "错链", "无关", "错配", "另一"), "link 阶段加同域概念消歧：候选含相近业务词时要求 LLM 比对 diff 实体名 vs 需求字段名，输出证据再关联。"),
        (("参照物", "link-only"), "intake 拦截 link-only 且 story_refs 失败的需求，要求人工补正文。"),
        (("覆盖", "未实现", "遗漏", "缺失", "不完整", "未闭环", "实现率"), "verify gate 增加覆盖度硬检查：需求验收点（spec 验收测试清单）逐条对 diff，未覆盖的显式列 missing 清单。"),
        (("范围", "失控", "蔓延"), "build gate 加 scope_drift 规则：diff 触及 spec 未声明 repo 时强制标注并进人工。"),
        (("理解", "解读", "锚点"), "intake/prd 生成阶段把需求拆成可勾选验收点（checklist），交付前 agent 自检勾选，减少理解偏差。"),
        (("偏差", "偏离", "不符", "错位"), "verify 阶段把『已实现部分一致性』与『需求完整度』分两个维度独立判定，避免口径混淆导致误判。"),
    ]
    lines += ["## 对 story-lifecycle 的改进建议（每主题一条）", ""]
    for theme, rs in sorted(by_theme.items(), key=lambda kv: -len(kv[1])):
        for kws, sug in suggestions:
            if any(k in theme for k in kws):
                lines.append(f"- **{theme}**（{len(rs)} 个）: {sug}")
                break
        else:
            lines.append(f"- **{theme}**（{len(rs)} 个）: 待人工分析后补建议。")
    lines.append("")

    path = RESULTS / "failure_patterns_20260805.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告: {path}", file=sys.stderr)


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    res = main()
    print(json.dumps(res, ensure_ascii=False))
