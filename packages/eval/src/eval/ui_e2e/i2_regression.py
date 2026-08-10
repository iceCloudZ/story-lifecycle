"""回归：迭代 2 改动后读路径不破坏（列表/详情/文档/gate-history）+ gate 回测抽 20。"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8181"
SHOTS = Path(r"D:\github\story-lifecycle\packages\eval\results\ui_e2e_shots_20260805")


def api_get(path, timeout=15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


results = {}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:150]}"))
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 500 and "/api/" in r.url else None)

    # 1. 列表 4 tab（等 refetch）
    counts = {}
    for name, path in (("待启动", "/"), ("开发中", "/dev"), ("测试·上线", "/test-release"), ("已结束", "/done")):
        page.goto(BASE + path, wait_until="domcontentloaded", timeout=30000)
        time.sleep(12)
        txt = page.inner_text("body")
        import re
        m = re.search(r"(\d+)\s*个 Story", txt)
        counts[name] = m.group(1) if m else "?"
    results["list_counts"] = counts
    print("list:", counts, flush=True)

    # 2. 详情抽查（construct-high-1 含面板 + ui-write-a-1）
    for key in ("construct-high-1", "ui-write-a-1", "ui-i2-g1"):
        page.goto(f"{BASE}/story/{key}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        txt = page.inner_text("body")
        results[f"detail_{key[-8:]}"] = {"chars": len(txt),
                                          "undef": any(w in txt for w in ("undefined", "NaN", "[object Object]")),
                                          "has_panel": "质量门禁" in txt}
        page.screenshot(path=str(SHOTS / f"i2_reg_{key[-8:]}.png"), full_page=True)
        print(key, "chars:", len(txt), "panel:", "质量门禁" in txt, flush=True)

    # 3. 文档渲染（A 样本 prd/spec）
    for doc in ("prd", "spec"):
        page.goto(f"{BASE}/story/tapd-1144381896001067103?tab=docs&doc={doc}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        txt = page.inner_text("body")
        results[f"docs_{doc}"] = {"chars": len(txt)}
        print("docs", doc, "chars:", len(txt), flush=True)

    # 4. gate-history API 200 + 字段
    gh = api_get("/api/story/construct-high-1/gate-history")
    d0 = (gh.get("decisions") or [{}])[0]
    results["gate_history"] = {"n": len(gh.get("decisions") or []),
                               "first_keys": sorted(d0.keys())}
    print("gate-history n:", results["gate_history"]["n"], flush=True)

    # 5. detail API 新字段
    d = api_get("/api/story/ui-i2-g1")
    results["detail_plan_fields"] = {"planConfirmed": d.get("planConfirmed"), "hasPlan": d.get("hasPlan")}
    print("detail planConfirmed/hasPlan:", d.get("planConfirmed"), d.get("hasPlan"), flush=True)

    results["errors"] = errors[:10]
    (Path(r"D:\github\story-lifecycle\packages\eval\results") / "i2_regression.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    browser.close()
