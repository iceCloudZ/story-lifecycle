"""手动驱动：打开 ui-write-a-1 详情页 → 点「开始执行」→ 观察生效与 agent 启动。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    events = []
    page.on("request", lambda r: events.append(f"REQ {r.method} {r.url}") if "/api/" in r.url else None)
    page.on("response", lambda r: events.append(f"RESP {r.status} {r.request.method} {r.url}") if "/api/" in r.url else None)
    page.on("dialog", lambda d: (events.append(f"DIALOG: {d.message[:100]}"), d.accept()))
    page.goto(f"{BASE}/story/ui-write-a-1", wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    print("buttons:", [b1.inner_text().strip() for b1 in page.locator("button").all() if b1.inner_text().strip()][:20])
    btn = page.get_by_role("button", name="开始执行")
    print("开始执行 visible:", btn.count() > 0)
    if btn.count():
        btn.first.click()
        time.sleep(8)
    print("=== events ===")
    for e in events[-25:]:
        print("  ", e)
    b.close()
