"""重测：story 计数 + API 请求时间线。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    events = []
    page.on("request", lambda r: events.append((time.monotonic(), "REQ", r.url)) if "/api/" in r.url else None)
    t0 = time.monotonic()
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    for i in range(18):
        time.sleep(1)
        m = page.locator(".story-count").first
        txt = ""
        try:
            txt = m.inner_text()
        except Exception:
            pass
        if i % 3 == 0 or "Story" in txt:
            print(f"t+{i+1}s story-count: {txt!r}")
    print("=== api events ===")
    for ts, kind, url in events:
        print(f"  +{ts - t0:.1f}s {kind} {url}")
    browser.close()
