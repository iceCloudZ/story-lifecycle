"""round 3 写路径：UI 创建 story → 规划 → 执行 → gate，全程 30s 轮询截图。

用法: python write_path.py <key> <title> <prd_text>
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")
SHOTS.mkdir(parents=True, exist_ok=True)
WS = Path(r"D:\github\story-lifecycle\packages\eval\sandbox-ui\ws")


def api_get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_story(page, key, cond, timeout_s, label=""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            s = api_get(f"/api/story/{key}")
            if cond(s):
                return s
        except Exception:
            pass  # server 同步阻塞规划期间 GET 可能超时——重试即可
        time.sleep(5)
    return None


def main():
    key, title, prd_text = sys.argv[1], sys.argv[2], sys.argv[3]
    results = {"key": key, "title": title}
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:200]}"))
        page.on("console", lambda m: errors.append(f"console[{m.type}]: {m.text[:150]}") if m.type in ("error",) else None)
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "/api/" in r.url else None)
        page.on("dialog", lambda d: d.accept())

        # 1. 打开首页 → 新建并开始
        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        page.get_by_role("button", name="新建并开始").first.click()
        time.sleep(1)

        # 2. 填表单
        page.get_by_placeholder("TAPD Story ID / Story Key").fill(key)
        page.get_by_placeholder("标题").fill(title)
        # profile: 选 eval-replay（select 下拉）
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            opts = sel.evaluate("el => [...el.options].map(o => o.value)")
            if "eval-replay" in opts:
                sel.select_option("eval-replay")
                break
            elif opts:
                results.setdefault("selects", {})[f"sel{i}"] = opts[:8]
        # 工作区/项目选择（优先沙箱内 workspace，禁止选 hc-all——铁律 hc-all 只读）
        for i in range(selects.count()):
            sel = selects.nth(i)
            opts = sel.evaluate("el => [...el.options].map(o => o.value)")
            if opts and "eval-replay" not in opts:
                pick = next((o for o in opts if "github" in o), None) or (opts[1] if len(opts) > 1 else opts[0])
                try:
                    sel.select_option(pick)
                    results.setdefault("selects", {})[f"sel{i}"] = pick
                except Exception:
                    pass
        # PRD 粘贴
        page.get_by_placeholder("粘贴需求 / PRD，或用上方按钮上传本地文件；后台会保存为 PRD.md").fill(prd_text)
        page.screenshot(path=str(SHOTS / f"write_{key[-8:]}_01_modal.png"), full_page=True)
        # 3. 准备 PRD 并进入规划
        page.get_by_role("button", name="准备 PRD 并进入规划").click()
        time.sleep(3)
        # 4. 等创建（story 存在）+ start 完成（intake_state=ready）
        def story_exists(key):
            try:
                s = api_get(f"/api/story/{key}")
                return bool(s)
            except Exception:
                return False
        created = wait_story(page, key, lambda s: True, 60)
        results["created"] = bool(created)
        if not created:
            results["error"] = "story 未创建"
            print(json.dumps(results, ensure_ascii=False))
            browser.close()
            return results
        print("created:", key)
        # 等 start（同步规划）完成：intake_state=ready（规划期间 server 同步阻塞,GET 可能超时,重试不算失败）
        def ready_or_planned(s):
            if s.get("intake_state") == "ready":
                return True
            ctx = json.loads(s.get("context_json") or "{}")
            return bool(ctx.get("_agent_actions"))
        ready = wait_story(page, key, ready_or_planned, 600, "start-plan")
        results["start_done"] = bool(ready)
        print("start_done:", results.get("start_done"))
        page.wait_for_url(f"**/story/{key}**", timeout=30000)
        time.sleep(5)
        page.screenshot(path=str(SHOTS / f"write_{key[-8:]}_02_detail.png"), full_page=True)

        # 5. 等规划完成（context 有 _agent_actions）

        # 6. 点「确认规划」(如按钮存在)
        confirm_candidates = ["开始执行", "确认规划", "确认并启动", "确认计划", "启动执行", "确认"]
        results["confirm_clicked"] = False
        for name in confirm_candidates:
            try:
                page.get_by_role("button", name=name).first.click(timeout=3000)
                results["confirm_clicked"] = True
                break
            except Exception:
                continue
        time.sleep(3)

        # 7. 30s 轮询 stage 变化 + 截图
        last_stage, last_status = "", ""
        t0 = time.monotonic()
        shot_i = 0
        while time.monotonic() - t0 < 2400:
            try:
                s = api_get(f"/api/story/{key}")
                stage, status = s.get("current_stage", ""), s.get("status", "")
                if (stage, status) != (last_stage, last_status):
                    print(f"[{(time.monotonic()-t0)/60:.1f}min] stage={stage} status={status}")
                    results.setdefault("transitions", []).append({"t_min": round((time.monotonic()-t0)/60, 1), "stage": stage, "status": status})
                    last_stage, last_status = stage, status
                    shot_i += 1
                    try:
                        page.goto(f"{BASE}/story/{key}", wait_until="domcontentloaded", timeout=30000)
                        page.screenshot(path=str(SHOTS / f"write_{key[-8:]}_{shot_i:02d}_{stage}.png"), full_page=True)
                    except Exception:
                        pass
                if status in ("completed", "failed", "aborted"):
                    results["final_status"] = status
                    break
            except Exception as e:
                errors.append(f"poll: {e}")
            time.sleep(30)

        # 8. gate 结果（DB gate_result）
        s = api_get(f"/api/story/{key}")
        results["final"] = {"stage": s.get("current_stage"), "status": s.get("status")}
        import sqlite3
        db = sqlite3.connect(str(WS.parent / "story_home" / "story.db"))
        cur = db.cursor()
        sid = cur.execute("SELECT id FROM story WHERE story_key=?", (key,)).fetchone()
        if sid:
            gates = cur.execute("SELECT stage, gate_name, result, detail FROM gate_result WHERE story_id=? ORDER BY id", (sid[0],)).fetchall()
            results["gates"] = [{"stage": g[0], "gate": g[1], "result": g[2], "detail": g[3][:300]} for g in gates]
        db.close()

        results["errors"] = errors[:30]
        (Path(r"D:\github\story-lifecycle\packages\eval\results") / f"ui_e2e_write_{key[-8:]}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        browser.close()
    return results


if __name__ == "__main__":
    main()

