"""写路径降级版：API 创建+规划+确认（绕过 UI intake 同步阻塞），UI 监控执行。

- API: POST /api/story (create) → POST /start (同步规划) → POST /plan/confirm
- UI: 打开详情页，每 30s 截图 + stage 记录，直到 completed/failed/超时
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")
SHOTS.mkdir(parents=True, exist_ok=True)


def api(method, path, body=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]


def main():
    key = "ui-write-a-1"
    title = "用户详情页新增职业字段展示"
    prd = open(r"C:\Users\zzh58\AppData\Local\Temp\opencode\prd_a.md", encoding="utf-8").read()
    results = {"key": key}
    errors = []

    # 1. create
    st, body = api("POST", "/api/story", {"key": key, "title": title, "profile": "eval-replay",
                                          "workspace": r"D:\github\story-lifecycle\packages\eval", "autostart": False})
    results["create"] = (st, body.get("storyKey") if isinstance(body, dict) else body)
    print("create:", results["create"])

    # 2. start（同步规划，可能 5-10min）
    t0 = time.monotonic()
    st, body = api("POST", f"/api/story/{key}/start", {"project_ids": [], "content": prd}, timeout=900)
    results["start"] = {"status": st, "t_s": round(time.monotonic() - t0), "body": body}
    print("start:", st, round(time.monotonic() - t0, 1), "s")

    # 3. confirm（确认规划 + 启动执行）
    st, body = api("POST", f"/api/story/{key}/plan/confirm", {}, timeout=60)
    results["confirm"] = (st, body)
    print("confirm:", results["confirm"])

    # 4. UI 监控
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:150]}"))
        page.on("console", lambda m: errors.append(f"console[{m.type}]: {m.text[:120]}") if m.type in ("error",) else None)
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "/api/" in r.url else None)

        page.goto(f"{BASE}/story/{key}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(8)
        page.screenshot(path=str(SHOTS / f"write_{key[-8:]}_00_start.png"), full_page=True)

        last_stage, last_status, shot_i = "", "", 0
        t0 = time.monotonic()
        transitions = []
        while time.monotonic() - t0 < 3600:
            try:
                s = api("GET", f"/api/story/{key}", timeout=15)[1]
                stage, status = s.get("current_stage", ""), s.get("status", "")
                if (stage, status) != (last_stage, last_status):
                    print(f"[{(time.monotonic()-t0)/60:.1f}min] stage={stage} status={status}")
                    transitions.append({"t_min": round((time.monotonic()-t0)/60, 1), "stage": stage, "status": status})
                    last_stage, last_status = stage, status
                    shot_i += 1
                    try:
                        page.goto(f"{BASE}/story/{key}", wait_until="domcontentloaded", timeout=30000)
                        page.screenshot(path=str(SHOTS / f"write_{key[-8:]}_{shot_i:02d}_{stage}.png"), full_page=True)
                    except Exception as e:
                        errors.append(f"shot: {e}")
                if status in ("completed", "failed", "aborted"):
                    results["final_status"] = status
                    break
            except Exception as e:
                errors.append(f"poll: {e}")
            time.sleep(30)

        results["transitions"] = transitions
        results["errors"] = errors[:30]
        # gate 结果
        import sqlite3
        db = sqlite3.connect(r"D:\github\story-lifecycle\packages\eval\sandbox-ui\story_home\story.db")
        cur = db.cursor()
        sid = cur.execute("SELECT id FROM story WHERE story_key=?", (key,)).fetchone()
        if sid:
            gates = cur.execute("SELECT stage, gate_name, result, detail FROM gate_result WHERE story_id=? ORDER BY id", (sid[0],)).fetchall()
            results["gates"] = [{"stage": g[0], "gate": g[1], "result": g[2], "detail": (g[3] or "")[:300]} for g in gates]
        db.close()
        (Path(r"D:\github\story-lifecycle\packages\eval\results") / f"ui_e2e_write_{key[-8:]}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
