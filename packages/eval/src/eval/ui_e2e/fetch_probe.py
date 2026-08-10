"""浏览器内验证：手动 fetch /api/story + 检查页面是否在 react-query Provider 内。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    reqs = []
    page.on("request", lambda r: reqs.append(f"{r.method} {r.url}"))
    page.goto(BASE, wait_until="networkidle", timeout=30000)
    time.sleep(12)  # 等一个 refetchInterval(10s) 周期

    print("=== requests in 12s (non-asset) ===")
    for r in reqs:
        if "assets/" not in r and ".woff" not in r:
            print("  ", r)
    print(f"total requests: {len(reqs)}")

    result = page.evaluate("""async () => {
        const out = {};
        try {
            const r = await fetch('/api/story');
            out.fetchStatus = r.status;
            out.count = (await r.json()).length;
        } catch (e) { out.fetchErr = String(e); }
        return out;
    }""")
    print("=== manual fetch ===", result)
    browser.close()
