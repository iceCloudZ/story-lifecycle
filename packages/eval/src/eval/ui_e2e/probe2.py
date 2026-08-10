"""UI 结构探测 v2：抓所有网络请求（含非 /api/）+ console + HTML dump。"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    reqs, resps, errs, consoles = [], [], [], []

    def on_req(r):
        reqs.append(f"{r.method} {r.url}")

    def on_resp(r):
        resps.append(f"{r.status} {r.request.method} {r.url}")

    def on_console(m):
        consoles.append(f"[{m.type}] {m.text[:150]}")

    def on_pageerror(e):
        errs.append(str(e)[:200])

    page.on("request", on_req)
    page.on("response", on_resp)
    page.on("console", on_console)
    page.on("pageerror", on_pageerror)

    page.goto(BASE, wait_until="networkidle", timeout=30000)
    time.sleep(3)
    page.screenshot(path=str(SHOTS / "01_home_v2.png"), full_page=True)

    print("=== REQUESTS ===")
    for r in reqs[:40]:
        print("  ", r)
    print(f"=== RESPONSES ({len(resps)}) ===")
    for r in resps[:40]:
        print("  ", r)
    print(f"=== CONSOLE ({len(consoles)}) ===")
    for c in consoles[:30]:
        print("  ", c)
    print(f"=== PAGE ERRORS ({len(errs)}) ===")
    for e in errs[:10]:
        print("  ", e)
    print("=== HTML (first 2500) ===")
    html = page.content()
    print(html[:2500])
    browser.close()
