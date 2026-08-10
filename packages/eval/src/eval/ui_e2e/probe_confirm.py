import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(f"{BASE}/story/ui-write-a-1", wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    print("=== buttons ===")
    for bt in page.locator("button").all():
        try:
            t = bt.inner_text().strip()
            if t:
                print("  ", repr(t[:70]))
        except Exception:
            pass
    print("=== body ===")
    print(page.inner_text("body")[:2000])
    b.close()
