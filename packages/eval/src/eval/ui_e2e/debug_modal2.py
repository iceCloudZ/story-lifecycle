"""调试 v2：完整 create+start 链路的请求/响应/console + 结果检查。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
PRD = open(r"C:\Users\zzh58\AppData\Local\Temp\opencode\prd_a.md", encoding="utf-8").read()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    events = []
    page.on("request", lambda r: events.append(f"REQ {r.method} {r.url}") if "/api/" in r.url else None)
    page.on("response", lambda r: events.append(f"RESP {r.status} {r.request.method} {r.url}") if "/api/" in r.url else None)
    page.on("console", lambda m: events.append(f"CONSOLE[{m.type}] {m.text[:120]}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: events.append(f"PAGEERR {str(e)[:200]}"))

    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    page.get_by_role("button", name="新建并开始").first.click()
    time.sleep(1)

    page.get_by_placeholder("TAPD Story ID / Story Key").fill("ui-write-a-1")
    page.get_by_placeholder("标题").fill("用户详情页新增职业字段展示")
    selects = page.locator("select")
    for i in range(selects.count()):
        opts = selects.nth(i).evaluate("el => [...el.options].map(o => o.value)")
        print(f"select{i}: {opts}")
    for i in range(selects.count()):
        opts = selects.nth(i).evaluate("el => [...el.options].map(o => o.value)")
        if "write-path" in opts:
            selects.nth(i).select_option("write-path")
        elif opts:
            pick = "D:\\hc-all" if "D:\\hc-all" in opts else opts[0]
            if i == 1 and "D:\\github" in opts:
                pick = [o for o in opts if "github" in o][0]
            selects.nth(i).select_option(pick)
            print(f"sel{i} -> {pick}")
    page.get_by_placeholder("粘贴需求 / PRD，或用上方按钮上传本地文件；后台会保存为 PRD.md").fill(PRD)
    time.sleep(1)
    btn = page.get_by_role("button", name="准备 PRD 并进入规划")
    print("submit disabled:", btn.is_disabled())
    btn.click()
    for _ in range(20):
        time.sleep(3)
        print(f"t+{(_+1)*3}s:", page.locator(".modal-prd-input").count() if page.locator(".modal-prd-input").count() else "modal closed?", "| url:", page.url[-40:])
        if page.url.endswith("/story/ui-write-a-1"):
            break
    time.sleep(3)
    print("=== events ===")
    for e in events[:50]:
        print("  ", e)
    print("=== url ===", page.url)
    b.close()
