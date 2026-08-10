"""dump 详情页 body 全文 + API 请求，确认渲染了什么。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
k = "tapd-1144381896001028664"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    reqs = []
    resps = []
    page.on("request", lambda r: reqs.append(r.url) if "/api/" in r.url else None)
    page.on("response", lambda r: resps.append(f"{r.status} {r.url}") if "/api/" in r.url else None)
    page.goto(f"{BASE}/story/{k}", wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    print("=== body text ===")
    print(page.inner_text("body")[:3000])
    print("=== api requests ===")
    for r in reqs:
        print("  ", r)
    print("=== responses ===")
    for r in resps:
        print("  ", r)
    browser.close()
