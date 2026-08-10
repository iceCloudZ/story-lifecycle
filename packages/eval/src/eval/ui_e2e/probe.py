"""UI 结构探测：打开 8181，截图首页 + dump 文本/链接/API 调用。"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")
SHOTS.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    api_calls = []
    page.on("response", lambda r: api_calls.append(f"{r.status} {r.url}") if "/api/" in r.url else None)
    page.on("console", lambda m: print(f"CONSOLE [{m.type}]: {m.text[:200]}") if m.type in ("error", "warning") else None)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE, wait_until="networkidle", timeout=30000)
    time.sleep(2)
    page.screenshot(path=str(SHOTS / "00_home.png"), full_page=True)
    print("=== title ===")
    print(page.title())
    print("=== body text (first 3000) ===")
    print(page.inner_text("body")[:3000])
    print("=== links ===")
    for a in page.locator("a").all()[:40]:
        try:
            print("  ", a.inner_text()[:60], "->", a.get_attribute("href"))
        except Exception:
            pass
    print("=== buttons ===")
    for b in page.locator("button").all()[:40]:
        try:
            print("  ", b.inner_text()[:60])
        except Exception:
            pass
    print("=== api calls ===")
    for c in api_calls[:30]:
        print("  ", c)
    print("=== page errors ===")
    for e in errors[:10]:
        print("  ", e)
    browser.close()
