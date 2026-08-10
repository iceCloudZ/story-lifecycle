import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(f"{BASE}/story/ui-write-a-1", wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    page.screenshot(path=SHOTS + r"\write_a1_paused_escalate.png", full_page=True)
    print(page.inner_text("body")[:2500])
    b.close()
