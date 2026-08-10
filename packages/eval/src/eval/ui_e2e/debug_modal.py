"""调试：modal 填表 + 提交过程的请求/响应/console。"""
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
    page.on("console", lambda m: events.append(f"CONSOLE[{m.type}] {m.text[:150]}") if m.type in ("error", "warning") else None)
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
    # profile
    for i in range(selects.count()):
        opts = selects.nth(i).evaluate("el => [...el.options].map(o => o.value)")
        if "write-path" in opts:
            selects.nth(i).select_option("write-path")
            break
    # workspace/project 默认选第一个非空
    for i in range(selects.count()):
        opts = selects.nth(i).evaluate("el => [...el.options].map(o => o.value)")
        if opts and "write-path" not in opts and opts[0]:
            print(f"auto-select sel{i} -> {opts[0]}")
            selects.nth(i).select_option(opts[0])
    page.get_by_placeholder("粘贴需求 / PRD，或用上方按钮上传本地文件；后台会保存为 PRD.md").fill(PRD)
    time.sleep(1)
    # 检查提交按钮状态
    btn = page.get_by_role("button", name="准备 PRD 并进入规划")
    print("submit disabled:", btn.is_disabled())
    page.screenshot(path=r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805\debug_modal.png", full_page=True)
    btn.click()
    time.sleep(8)
    print("=== events ===")
    for e in events[:40]:
        print("  ", e)
    print("=== body tail ===")
    print(page.inner_text("body")[-800:])
    b.close()
