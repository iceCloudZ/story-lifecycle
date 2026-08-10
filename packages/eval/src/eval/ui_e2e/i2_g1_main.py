"""G1 验收：UI 主链路全点通——创建 → 规划完成 → 「确认规划」按钮 → 「开始执行」→ agent 启动。

全链路无 API 降级（不用 API 建 story/确认）。/start 同步规划期间 server 阻塞是
已知体验缺口（记录，非本轮修复目标）——脚本轮询等待规划完成。
"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")
KEY = "ui-i2-g1"
TITLE = "用户详情页新增职业字段展示"
PRD = open(r"C:\Users\zzh58\AppData\Local\Temp\opencode\prd_a.md", encoding="utf-8").read()


def api_get(path, timeout=15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


results = {"key": KEY}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:150]}"))
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "/api/" in r.url else None)
    page.on("dialog", lambda d: d.accept())

    # 1. UI 创建（modal 全流程）
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    page.get_by_role("button", name="新建并开始").first.click()
    time.sleep(1)
    page.get_by_placeholder("TAPD Story ID / Story Key").fill(KEY)
    page.get_by_placeholder("标题").fill(TITLE)
    selects = page.locator("select")
    for i in range(selects.count()):
        opts = selects.nth(i).evaluate("el => [...el.options].map(o => o.value)")
        if "eval-replay" in opts:
            selects.nth(i).select_option("eval-replay")
        elif opts:
            pick = next((o for o in opts if "github" in o), None) or opts[0]
            selects.nth(i).select_option(pick)
    page.get_by_placeholder("粘贴需求 / PRD，或用上方按钮上传本地文件；后台会保存为 PRD.md").fill(PRD)
    time.sleep(1)
    btn = page.get_by_role("button", name="准备 PRD 并进入规划")
    results["submit_disabled"] = btn.is_disabled()
    btn.click()
    time.sleep(3)
    results["modal_submitted"] = True
    page.screenshot(path=str(SHOTS / "i2_g1_01_modal.png"), full_page=True)

    # 2. 等规划完成（/start 同步规划，轮询 detail.hasPlan）
    t0 = time.monotonic()
    plan_done = False
    while time.monotonic() - t0 < 900:
        try:
            s = api_get(f"/api/story/{KEY}")
            if s.get("hasPlan") and not s.get("planConfirmed"):
                plan_done = True
                break
            if s.get("planConfirmed"):
                plan_done = True
                break
        except Exception:
            pass
        time.sleep(10)
    results["plan_done_wait_s"] = round(time.monotonic() - t0)
    results["plan_done"] = plan_done
    print("plan done:", plan_done, "after", results["plan_done_wait_s"], "s", flush=True)

    # 3. 详情页：断言「确认规划」按钮出现（Bug #4 修复点）
    page.goto(f"{BASE}/story/{KEY}", wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    page.screenshot(path=str(SHOTS / "i2_g1_02_detail.png"), full_page=True)
    confirm_btn = page.get_by_role("button", name="确认规划")
    results["confirm_btn_visible"] = confirm_btn.count() > 0 and confirm_btn.first.is_visible()
    print("confirm btn visible:", results["confirm_btn_visible"], flush=True)

    # 4. 点「确认规划」（TerminalTab 的确认按钮）→ agent 启动
    if results["confirm_btn_visible"]:
        confirm_btn.first.click()
        time.sleep(3)
        results["confirm_clicked"] = True
        # 等 detail.planConfirmed 变 True + agent spawn（stage_log / sessions）
        t0 = time.monotonic()
        confirmed = False
        while time.monotonic() - t0 < 60:
            try:
                s = api_get(f"/api/story/{KEY}")
                if s.get("planConfirmed"):
                    confirmed = True
                    break
            except Exception:
                pass
            time.sleep(3)
        results["plan_confirmed_after_click"] = confirmed
        print("planConfirmed after click:", confirmed, flush=True)

    # 5. 等 agent spawn（编排线程自动 spawn——eval-replay 全自动）
    t0 = time.monotonic()
    spawned = False
    while time.monotonic() - t0 < 180:
        try:
            s = api_get(f"/api/story/{KEY}")
            ctx = json.loads(s.get("contextJson") or "{}")
            if ctx.get("_active_execution"):
                spawned = True
                break
        except Exception:
            pass
        time.sleep(5)
    results["agent_spawned"] = spawned
    print("agent spawned:", spawned, flush=True)
    time.sleep(5)
    page.goto(f"{BASE}/story/{KEY}", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    page.screenshot(path=str(SHOTS / "i2_g1_03_running.png"), full_page=True)

    results["errors"] = errors[:20]
    (Path(r"D:\github\story-lifecycle\packages\eval\results") / "i2_g1_main_flow.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    browser.close()
