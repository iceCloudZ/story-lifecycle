"""UI 构造样本读路径检查（sandbox-ui DB）：HIGH/swap/缺依赖三类 + v2 样本 gate 展示。

重点截图：详情页 overview（finding/gate 相关展示）。
"""
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")

CASES = [
    ("construct-high-1", "HIGH finding: conformance"),
    ("construct-high-2", "HIGH finding: test"),
    ("construct-swap-1", "swap_approach opencode→kimi"),
    ("construct-swap-2", "swap_approach codex→claude"),
    ("construct-rescue-1", "缺依赖 done"),
    ("construct-rescue-2", "极端缺依赖"),
    ("tapd-1144381896001028664", "v2 样本 B (gate rework)"),
    ("tapd-1144381896001067103", "v2 样本 A (gate pass)"),
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    results = {}
    for k, label in CASES:
        reqs = []
        page.on("request", lambda r, u=reqs: u.append(r.url) if "/api/" in r.url else None)
        page.goto(f"{BASE}/story/{k}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(7)
        txt = page.inner_text("body")
        # 关键词检测
        found = {
            "finding": "finding" in txt.lower() or "质量" in txt or "问题" in txt,
            "high": "high" in txt.lower() or "高" in txt,
            "gate": any(w in txt for w in ("gate", "verdict", "rework", "retry", "advance")),
            "repair": "swap" in txt.lower() or "adapter" in txt.lower() or "救援" in txt or "恢复" in txt,
            "undef": any(w in txt for w in ("undefined", "NaN", "[object Object]")),
        }
        page.screenshot(path=str(SHOTS / f"construct_{k[-10:]}.png"), full_page=True)
        # 文档 tab 截图（swap-1 的 playbook 不直接展示,截 docs tab 的 PRD）
        page.goto(f"{BASE}/story/{k}?tab=docs&doc=prd", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        page.screenshot(path=str(SHOTS / f"construct_{k[-10:]}_docs.png"), full_page=True)
        results[k] = {"label": label, "chars": len(txt), "found": found, "api_count": len(reqs)}
        print(f"{k[-14:]}: {label} | chars={len(txt)} found={found}")
        page.close()  # 清请求监听
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
    (Path(r"D:\github\story-lifecycle\packages\eval\results") / "ui_e2e_construct_20260805.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()
