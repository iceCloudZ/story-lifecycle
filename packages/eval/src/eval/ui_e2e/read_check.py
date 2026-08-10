"""round 3 读路径检查（round 2 DB，8181）：列表/详情/文档/gate/日志。

- 列表页: 20+ story 可见（等 refetch 12s）
- 详情页: 抽查 5 个 overview 状态 vs DB
- 文档: A/B/C/D 每类 ≥1 个 PRD/spec 渲染
- gate: B 类 5 个 story 的 gate 结果可见
- 截图存 results/ui_e2e_shots_20260805/
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")
SHOTS.mkdir(parents=True, exist_ok=True)

A = ["tapd-1144381896001067103", "tapd-1144381896001062996", "tapd-1144381896001062040",
     "tapd-1144381896001063633", "tapd-1144381896001060008"]
B = ["tapd-1144381896001028664", "tapd-1144381896001067383", "tapd-1144381896001033047",
     "tapd-1144381896001034546", "tapd-1144381896001063418"]
C = ["tapd-1144381896001065519", "tapd-1144381896001065191", "tapd-1144381896001065618",
     "tapd-1144381896001066171", "tapd-1144381896001065520"]
D = ["tapd-1144381896001065500", "tapd-1144381896001066752", "tapd-1144381896001064837",
     "tapd-1144381896001067804", "tapd-1144381896001065458"]
ALL = A + B + C + D


def main() -> dict:
    results = {"list": {}, "detail": {}, "docs": {}, "gate": {}}
    consoles, errors = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: consoles.append(f"[{m.type}] {m.text[:150]}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))

        # ---- 1. 列表页 ----
        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        time.sleep(13)  # 等 refetchInterval(10s) 数据上屏
        counts = {}
        for name, path in (("待启动", "/"), ("开发中", "/dev"), ("测试·上线", "/test-release"), ("已结束", "/done")):
            page.goto(BASE + path, wait_until="domcontentloaded", timeout=30000)
            time.sleep(12)
            txt = page.inner_text("body")
            m = re.search(r"(\d+)\s*个 Story", txt)
            counts[name] = m.group(1) if m else "?"
            page.screenshot(path=str(SHOTS / f"list_{name}.png"), full_page=True)
        results["list"] = counts
        print("list counts:", counts)

        # ---- 2. 详情页抽查 5 个 ----
        check_keys = ["tapd-1144381896001067103", "tapd-1144381896001067383",
                      "tapd-1144381896001065519", "tapd-1144381896001065500", "tapd-1144381896001028664"]
        for k in check_keys:
            page.goto(f"{BASE}/story/{k}", wait_until="domcontentloaded", timeout=30000)
            time.sleep(7)
            txt = page.inner_text("body")
            has_undef = any(w in txt for w in ("undefined", "NaN", "[object Object]"))
            page.screenshot(path=str(SHOTS / f"detail_{k[-8:]}.png"), full_page=True)
            results["detail"][k] = {"chars": len(txt), "has_undef": has_undef}
            print(f"detail {k[-12:]}: chars={len(txt)} undef={has_undef}")

        # ---- 3. 文档渲染：A/B/C/D 每类 1 个 ----
        doc_samples = {"A": A[0], "B": B[0], "C": C[0], "D": D[0]}
        for cls, k in doc_samples.items():
            got = {}
            for doc in ("prd", "spec"):
                page.goto(f"{BASE}/story/{k}?tab=docs&doc={doc}", wait_until="domcontentloaded", timeout=30000)
                time.sleep(7)
                txt = page.inner_text("body")
                has_md = ("#" in txt) or ("```" in txt) or ("##" in txt)
                got[doc] = {"chars": len(txt), "has_md_markers": has_md}
                page.screenshot(path=str(SHOTS / f"docs_{cls}_{doc}.png"), full_page=True)
            results["docs"][cls] = got
            print(f"docs {cls} ({k[-10:]}): {got}")

        # ---- 4. gate 结果：B 类 5 个（记录 API 请求有无 gate-history + 页面 gate 词）----
        for k in B:
            req_urls = []
            page.on("request", lambda r, u=req_urls: u.append(r.url) if "/api/" in r.url else None)
            page.goto(f"{BASE}/story/{k}?tab=overview", wait_until="domcontentloaded", timeout=30000)
            time.sleep(7)
            txt = page.inner_text("body")
            gate_words = [w for w in ("gate", "Gate", "verdict", "retry", "rework", "advance", "fail", "findings") if w in txt]
            has_gate_history_api = any("gate-history" in u for u in req_urls)
            page.screenshot(path=str(SHOTS / f"gate_{k[-8:]}.png"), full_page=True)
            results["gate"][k] = {"gate_words": gate_words, "gate_history_api_called": has_gate_history_api,
                                  "chars": len(txt)}
            print(f"gate {k[-12:]}: words={gate_words} gate-history-api={has_gate_history_api}")

        results["consoles"] = consoles[:15]
        results["page_errors"] = errors[:10]
        (Path(r"D:\github\story-lifecycle\packages\eval\results") / "ui_e2e_read_20260805.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
    return results


if __name__ == "__main__":
    main()
