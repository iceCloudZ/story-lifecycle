"""探详情页（刚创建的 story）的按钮：确认规划/启动入口。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(f"{BASE}/story/construct-high-1", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    print("=== buttons ===")
    for bt in page.locator("button").all():
        try:
            t = bt.inner_text().strip()
            if t:
                print("  ", repr(t[:60]))
        except Exception:
            pass
    b.close()
