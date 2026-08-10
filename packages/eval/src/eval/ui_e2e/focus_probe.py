"""决定性实验：headless vs headed + document.hasFocus() + 网络请求对比。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

for mode in ("headless", "headed"):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=(mode == "headless"))
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        reqs = []
        page.on("request", lambda r: reqs.append(r.url))
        page.goto(BASE, wait_until="networkidle", timeout=30000)
        time.sleep(13)
        focus = page.evaluate("document.hasFocus()")
        vis = page.evaluate("document.visibilityState")
        text = page.inner_text("body")
        import re

        m = re.search(r"(\d+)\s*个Story", text.replace("\n", ""))
        api_reqs = [u for u in reqs if "/api/" in u]
        print(f"=== {mode}: hasFocus={focus} visibility={vis} story_count={m.group(1) if m else '?'} api_requests={len(api_reqs)}")
        for u in api_reqs[:5]:
            print("   ", u)
        browser.close()
