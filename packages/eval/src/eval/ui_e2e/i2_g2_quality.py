"""G2 验收：质量面板三类构造样本断言可见（HIGH/swap/缺依赖 + escalate + [FALLBACK]）。"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")

CASES = [
    ("construct-high-1", {"findings": True, "high": True}),
    ("construct-swap-1", {"repair_swap": True}),
    ("construct-rescue-1", {"repair_rescue": True}),
    ("ui-write-a-1", {"decisions_ge_3": True, "escalate": True}),
    ("construct-high-1", {"fallback_tag": True}),
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    results = {}
    for key, expects in CASES:
        page.goto(f"{BASE}/story/{key}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        panel = page.locator('[data-testid="qp-panel"]')
        visible = panel.count() > 0 and panel.first.is_visible()
        out = {"panel_visible": visible}
        if visible:
            out["findings"] = page.locator('[data-testid="qp-finding"]').count()
            out["decisions"] = page.locator('[data-testid="qp-decision"]').count()
            out["high_count"] = page.locator('[data-testid="qp-high-count"]').count()
            out["fallback_tags"] = page.locator('[data-testid="qp-fallback-tag"]').count()
            out["repair_tags"] = [t.inner_text() for t in page.locator('[data-testid="qp-repair"]').all()]
            out["escalate"] = page.locator('[data-testid="qp-decision"].qp-escalate').count()
            body = page.inner_text("body")
            out["body_has_quality"] = "质量门禁" in body
        ok = True
        if expects.get("findings"):
            ok = ok and out.get("findings", 0) >= 1
        if expects.get("high"):
            ok = ok and out.get("high_count", 0) >= 1
        if expects.get("repair_swap"):
            ok = ok and any("swap" in t for t in out.get("repair_tags", []))
        if expects.get("repair_rescue"):
            ok = ok and any("rescue" in t for t in out.get("repair_tags", []))
        if expects.get("decisions_ge_3"):
            ok = ok and out.get("decisions", 0) >= 3
        if expects.get("escalate"):
            ok = ok and out.get("escalate", 0) >= 1
        if expects.get("fallback_tag"):
            ok = ok and out.get("fallback_tags", 0) >= 1
        out["ok"] = ok
        results[key] = out
        page.screenshot(path=str(SHOTS / f"i2_qp_{key[-10:]}.png"), full_page=True)
        print(key, "->", json.dumps(out, ensure_ascii=False))
    (Path(r"D:\github\story-lifecycle\packages\eval\results") / "i2_g2_quality_panel.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()
