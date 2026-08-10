"""写路径 UI 监控：每 30s 截图 + stage 记录，直到 completed/failed/超时 90min。"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")
KEY = "ui-write-a-1"


def api_get(path, timeout=15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:150]}"))
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "/api/" in r.url else None)
    page.goto(f"{BASE}/story/{KEY}", wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    page.screenshot(path=str(SHOTS / f"write_{KEY[-8:]}_00_start.png"), full_page=True)

    last = ("", "")
    transitions = []
    t0 = time.monotonic()
    shot_i = 0
    while time.monotonic() - t0 < 5400:
        try:
            s = api_get(f"/api/story/{KEY}")
            cur = (s.get("current_stage", ""), s.get("status", ""))
            if cur != last:
                print(f"[{(time.monotonic()-t0)/60:.1f}min] stage={cur[0]} status={cur[1]}", flush=True)
                transitions.append({"t_min": round((time.monotonic()-t0)/60, 1), "stage": cur[0], "status": cur[1]})
                last = cur
                shot_i += 1
                try:
                    page.goto(f"{BASE}/story/{KEY}", wait_until="domcontentloaded", timeout=30000)
                    page.screenshot(path=str(SHOTS / f"write_{KEY[-8:]}_{shot_i:02d}_{cur[0] or 'x'}.png"), full_page=True)
                except Exception as e:
                    errors.append(f"shot: {e}")
            if cur[1] in ("completed", "failed", "aborted"):
                break
        except Exception as e:
            errors.append(f"poll: {e}")
        time.sleep(30)
    results = {"key": KEY, "transitions": transitions, "errors": errors[:30]}
    (Path(r"D:\github\story-lifecycle\packages\eval\results") / "ui_e2e_write_monitor.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False), flush=True)
    browser.close()
