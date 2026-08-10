"""监听页面自身 WS 的帧收发 + console 全量。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    ws_frames = []
    consoles = []

    def on_ws(ws):
        u = ws.url
        ws.on("framesent", lambda f: ws_frames.append(f"SENT {u}: {f[:200]}"))
        ws.on("framereceived", lambda f: ws_frames.append(f"RECV {u}: {f[:400]}"))

    page.on("websocket", on_ws)
    page.on("console", lambda m: consoles.append(f"[{m.type}] {m.text[:200]}"))
    page.goto(BASE, wait_until="networkidle", timeout=30000)
    time.sleep(6)

    print("=== WS frames ===")
    for f in ws_frames:
        print(" ", f)
    print(f"=== console ({len(consoles)}) ===")
    for c in consoles[:30]:
        print(" ", c)
    print("=== body count ===")
    t = page.inner_text("body")
    import re
    m = re.search(r"(\d+) 个?Story", t)
    print("story count text:", m.group(0) if m else "not found")
    browser.close()
