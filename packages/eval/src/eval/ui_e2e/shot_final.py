"""补截图：A/B 类 paused 详情页 + 列表页（escalate 展示）。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    for key, tag in (("ui-write-a-1", "write_a1"), ("ui-write-b-1", "write_b1")):
        page.goto(f"{BASE}/story/{key}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        page.screenshot(path=f"{SHOTS}\\{tag}_paused.png", full_page=True)
        txt = page.inner_text("body")
        print(f"=== {key} ===")
        print(txt[:1800])
        print("...")
    # 列表页
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    time.sleep(12)
    page.screenshot(path=f"{SHOTS}\\write_list.png", full_page=True)
    b.close()
