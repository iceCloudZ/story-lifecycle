"""直接连 /ws/stories 看后端推送内容 + UI 的 store 状态。"""
import json
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    ws_msgs = []
    page.on("websocket", lambda ws: ws.on("framesent", lambda f: None))
    page.on("response", lambda r: print("RESP", r.status, r.url) if "/api/" in r.url else None)
    page.goto(BASE, wait_until="networkidle", timeout=30000)
    time.sleep(3)

    # 独立 WS 客户端
    got = page.evaluate("""
        () => new Promise((resolve) => {
            const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/stories`);
            const msgs = [];
            ws.onopen = () => msgs.push('OPEN');
            ws.onmessage = (e) => { msgs.push('MSG: ' + e.data.slice(0, 300)); resolve(msgs); };
            ws.onerror = (e) => { msgs.push('ERR'); resolve(msgs); };
            setTimeout(() => resolve(msgs), 8000);
        })
    """)
    print("=== WS direct ===")
    for m in got:
        print(" ", m[:400])

    # UI 页面 store 状态（读 dashboard 计数 + 是否有关键错误）
    text = page.inner_text("body")
    print("=== body text ===")
    print(text[:800])
    browser.close()
