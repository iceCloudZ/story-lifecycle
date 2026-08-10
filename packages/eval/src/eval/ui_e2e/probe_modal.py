"""探「新建并开始」modal 的表单元素。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    page.get_by_role("button", name="新建并开始").click()
    time.sleep(1)
    print("=== modal body text ===")
    print(page.inner_text("body")[-2500:])
    print("=== inputs ===")
    for i in page.locator("input, textarea, select").all():
        print("  ", i.get_attribute("placeholder") or i.get_attribute("type") or i.get_attribute("name") or i.evaluate("el => el.className")[:60])
    b.close()
