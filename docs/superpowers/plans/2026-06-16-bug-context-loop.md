# Bug 上下文闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通"改 bug 闭环"——进 story 详情自动同步关联 bug、pack(bug) 含关联需求 context、改完 resolve 收尾 + bugfix-report 证据。

**Architecture:** bug↔需求关联从 story 侧拿（TAPD get-related-bugs），进 story 详情触发同步（节流）；pack 解析 bug.parent_key 拼需求 context；resolve 端点收尾 TAPD + 本地状态；bugfix-report 结构化三节（document ref = 数据飞轮 P2 数据源）。

**Tech Stack:** Python/FastAPI/SQLite（后端 TDD）；React 19/TS（前端 build 验证）。

**Conventions:** 所有 commit 以 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` 结尾。直接提交 main。后端命令在 `D:\story-lifecycle`，前端 `frontend/`。

---

## File Structure

**后端（`src/story_lifecycle/`）**
- Modify: `sources/tapd_api.py` — 加 `get_related_bugs`
- Modify: `db/models.py` — `upsert_story_from_source` 加 `parent_key` 参数
- Modify: `orchestrator/api.py` — 加 `POST /sync-related-bugs`、`POST /resolve`、`GET /context/pack?skill=`
- Modify: `orchestrator/context/pack.py` — parent 解析 + skill 提示词 + 完整度检查

**测试（`tests/`）**
- Create: `tests/test_sync_related_bugs.py`、`tests/test_resolve.py`
- Modify: `tests/test_context_pack.py`（加 parent/skill/完整度测试）

**前端（`frontend/src/`）**
- Modify: `pages/StoryDetailPage.tsx` — 进详情触发 sync-related-bugs（节流）
- Modify: `components/ContextTab.tsx` — skill 下拉 + 完整度显示
- Modify: `components/StorySidebar.tsx` — bug 详情「标记已修复」按钮

---

## Task 1: TapdApi.get_related_bugs

**Files:** Modify `src/story_lifecycle/sources/tapd_api.py`；Test `tests/test_sync_related_bugs.py`

- [ ] **Step 1: 写失败测试** — create `tests/test_sync_related_bugs.py`:

```python
"""Tests for sync-related-bugs (TapdApi + endpoint)."""
from story_lifecycle.sources.tapd_api import TapdApi


def test_get_related_bugs_calls_cli_with_story_id(monkeypatch):
    api = TapdApi(workspace_id="44381896")
    calls = []

    def fake_call(cmd, params):
        calls.append((cmd, params))
        return {"data": [{"bug_id": "B1", "story_id": "S1"}]}

    monkeypatch.setattr(api, "_call", fake_call)
    result = api.get_related_bugs("S1")
    assert result == [{"bug_id": "B1", "story_id": "S1"}]
    assert calls == [("get_related_bugs", {"story_id": "S1"})]
```

- [ ] **Step 2: 跑失败** — `python -m pytest tests/test_sync_related_bugs.py -v` → FAIL (AttributeError: get_related_bugs)

- [ ] **Step 3: 实现** — in `tapd_api.py`, after `get_entity_relations`:

```python
    def get_related_bugs(self, story_id: str) -> list[dict]:
        """Bugs linked to a story (TAPD stories/get_related_bugs)."""
        result = self._call("get_related_bugs", {"story_id": story_id})
        if isinstance(result, dict):
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return result if isinstance(result, list) else []
```

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_sync_related_bugs.py -v` → 1 passed

- [ ] **Step 5: Commit** — `git add src/story_lifecycle/sources/tapd_api.py tests/test_sync_related_bugs.py && git commit -m "feat(tapd): TapdApi.get_related_bugs" `（加 trailer）

---

## Task 2: upsert_story_from_source 加 parent_key

**Files:** Modify `src/story_lifecycle/db/models.py:771`；Test `tests/test_sync_related_bugs.py`（追加）

- [ ] **Step 1: 追加测试**

```python
from story_lifecycle.db import models as db


def test_upsert_bug_sets_parent_key(isolated_story_home, tmp_path):
    db.create_story(story_key="REQ-1", title="需求", workspace=str(tmp_path))
    story, _ = db.upsert_story_from_source(
        source_type="tapd", source_id="bug_1009779",
        title="客户UID千分位", tapd_type="bug", parent_key="REQ-1",
    )
    assert story["parent_key"] == "REQ-1"
    # update path also sets parent_key
    db.upsert_story_from_source(
        source_type="tapd", source_id="bug_1009779", parent_key="REQ-1",
    )
    assert db.get_story(story["story_key"])["parent_key"] == "REQ-1"
```

- [ ] **Step 2: 跑失败** — `python -m pytest tests/test_sync_related_bugs.py::test_upsert_bug_sets_parent_key -v` → FAIL (TypeError: unexpected parent_key)

- [ ] **Step 3: 实现** — in `models.py`, `upsert_story_from_source` 签名加 `parent_key: str = "",`（在 `tapd_type` 后）。在 update 分支 `if parent_key: updates["parent_key"] = parent_key`（在 `if tapd_type` 块后）。在 create 分支把 `parent_key=parent_key` 传给 `create_story(...)`。

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_sync_related_bugs.py -v` → 2 passed

- [ ] **Step 5: Commit** — `feat(db): upsert_story_from_source supports parent_key`

---

## Task 3: POST /sync-related-bugs 端点

**Files:** Modify `src/story_lifecycle/orchestrator/api.py`（在 context 端点附近）；Test `tests/test_sync_related_bugs.py`（追加）

- [ ] **Step 1: 追加测试**

```python
from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.api import app


def test_sync_related_bugs_upserts_with_parent(monkeypatch, isolated_story_home, tmp_path):
    db.create_story(story_key="tapd-1065460", title="删除联系人",
                    workspace=str(tmp_path), source_type="tapd", source_id="1144381896001065460")
    db.create_story  # noqa
    # 让 get_story 返回 source_type/source_id（create_story 默认不设 source，用 upsert 重设）
    db.upsert_story_from_source(source_type="tapd", source_id="1144381896001065460",
                                title="删除联系人", tapd_type="story")
    key = db.find_by_source_id("tapd", "1144381896001065460")["story_key"]

    import story_lifecycle.orchestrator.api as api_mod

    class FakeApi:
        def get_related_bugs(self, sid):
            return [{"bug_id": "1144381896001009779", "story_id": sid}]
        def get_bug_detail(self, bid):
            return {"Bug": {"title": "客户UID千分位", "status": "new", "current_owner": "赵子豪;"}}

    monkeypatch.setattr(api_mod, "_load_tapd_config", lambda: {"workspace_id": "44381896"})
    monkeypatch.setattr("story_lifecycle.sources.tapd_api.TapdApi", lambda **kw: FakeApi())

    client = TestClient(app)
    r = client.post(f"/api/story/{key}/sync-related-bugs")
    assert r.status_code == 200
    assert r.json()["synced"] == 1
    bug = db.find_by_source_id("tapd", "bug_1144381896001009779")
    assert bug["parent_key"] == key
    assert bug["tapd_type"] == "bug"


def test_sync_related_bugs_404_unknown(isolated_story_home):
    client = TestClient(app)
    assert client.post("/api/story/NOPE/sync-related-bugs").status_code == 404
```

- [ ] **Step 2: 跑失败** — 404（端点不存在）

- [ ] **Step 3: 实现** — in `api.py` 顶部 import 区加 `from ..cli.sync_cmd import _load_tapd_config` 与 `from ..sources.tapd_api import TapdApi`（放函数内 import 亦可，但测试 monkeypatch `story_lifecycle.sources.tapd_api.TapdApi`，故用模块级 import）。在 context 端点附近加：

```python
@app.post("/api/story/{story_key}/sync-related-bugs")
def api_sync_related_bugs(story_key: str):
    """Sync bugs linked to this story (via TAPD get_related_bugs), setting parent_key."""
    story = db.get_story(story_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    if story.get("source_type") != "tapd":
        return {"synced": 0, "reason": "not a tapd source"}
    config = _load_tapd_config()
    if not config.get("workspace_id"):
        raise HTTPException(status_code=503, detail="TAPD not configured")
    api = TapdApi(workspace_id=config["workspace_id"])
    related = api.get_related_bugs(story["source_id"]) or []
    synced = 0
    for r in related:
        bug_id = r.get("bug_id")
        if not bug_id:
            continue
        flat = (api.get_bug_detail(bug_id) or {}).get("Bug", {})
        db.upsert_story_from_source(
            source_type="tapd",
            source_id=f"bug_{bug_id}",
            title=flat.get("title", ""),
            tapd_type="bug",
            tapd_status=flat.get("status", ""),
            owner=flat.get("current_owner", ""),
            tapd_url=f"https://www.tapd.cn/{config['workspace_id']}/bugtrace/bugs/view?bug_id={bug_id}",
            parent_key=story_key,
        )
        synced += 1
    return {"synced": synced, "story_key": story_key}
```

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_sync_related_bugs.py -v` → 4 passed

- [ ] **Step 5: Commit** — `feat(api): POST /sync-related-bugs endpoint`

---

## Task 4: pack 解析 parent_key（拼关联需求 context）

**Files:** Modify `src/story_lifecycle/orchestrator/context/pack.py`；Test `tests/test_context_pack.py`（追加）

- [ ] **Step 1: 追加测试**

```python
def test_pack_includes_parent_requirement(isolated_story_home, tmp_path):
    # parent 需求
    db.create_story(story_key="REQ-1", title="删除联系人", workspace=str(tmp_path))
    db.create_project(name="p", repo_path=str(tmp_path))
    db.bind_story_project("REQ-1", 1, branch="feature/x")
    db.create_document("REQ-1", kind="spec", ref="spec.md", summary="联系人删除")
    # bug with parent
    db.create_story(story_key="BUG-9", title="UID千分位", workspace=str(tmp_path), parent_key="REQ-1")
    content = generate_pack("BUG-9")["content"]
    assert "关联需求" in content
    assert "删除联系人" in content          # parent title
    assert "feature/x" in content          # parent branch
    assert "spec.md" in content            # parent spec ref
```

- [ ] **Step 2: 跑失败** — FAIL（pack 无 parent 节）

- [ ] **Step 3: 实现** — in `pack.py` `_render_pack`，在「交付产物」渲染之后、return 之前加：

```python
    # 关联需求（parent）—— bug 改时带上需求的 spec/plan/分支/DDL
    parent_key = story.get("parent_key", "")
    if parent_key:
        try:
            parent_bundle = ContextResolver().resolve(parent_key)
            pst = parent_bundle.story or {}
            lines.append("")
            lines.append(f"## 关联需求：{pst.get('title', parent_key)}")
            if pst.get("tapd_url"):
                lines.append(f"- TAPD：{pst['tapd_url']}")
            for sp in parent_bundle.story_projects:
                proj = _find_project(parent_bundle.projects, sp.get("project_id"))
                pname = proj.get("name", "") if proj else ""
                lines.append(f"- **{pname}**：分支 `{sp.get('branch', '')}`")
            for doc in parent_bundle.documents:
                lines.append(f"- {doc.get('kind', '')}：{doc.get('ref', '')}")
            for ci in parent_bundle.change_items:
                lines.append(f"- {ci.get('kind', '').upper()}：{ci.get('ref', '') or ci.get('summary', '')}")
        except Exception:
            lines.append("")
            lines.append(f"## 关联需求：{parent_key}（详情加载失败）")
```

（`ContextResolver` 已在 pack.py 顶部 import）

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_context_pack.py -v` → 全过

- [ ] **Step 5: Commit** — `feat(pack): include parent requirement context`

---

## Task 5: pack 可选 skill 提示词

**Files:** Modify `pack.py`（`generate_pack` + `_render_pack`）+ `api.py`（端点 skill 参数）；Test `tests/test_context_pack.py`（追加）

- [ ] **Step 1: 追加测试**

```python
def test_pack_skill_hint_when_param(isolated_story_home, tmp_path):
    db.create_story(story_key="S-skill", title="t", workspace=str(tmp_path))
    content = generate_pack("S-skill", skill="bug-fix")["content"]
    assert "建议调用 /bug-fix 处理" in content


def test_pack_no_skill_hint_by_default(isolated_story_home, tmp_path):
    db.create_story(story_key="S-noskill", title="t", workspace=str(tmp_path))
    content = generate_pack("S-noskill")["content"]
    assert "建议调用" not in content
```

- [ ] **Step 2: 跑失败** — FAIL（generate_pack 不接受 skill）

- [ ] **Step 3: 实现**
  - `generate_pack(story_key, skill="")`：把 `skill` 传给 `_render_pack`。
  - `_render_pack(story_key, bundle, skill="")`：在头部构建后（`# Story 上下文资料包` 之后）插入：
    ```python
    if skill:
        lines.insert(1, "")
        lines.insert(1, f"## 建议调用 /{skill} 处理")
    ```
  - `api.py` 端点加 skill 参数：`def api_get_context_pack(story_key: str, skill: str = ""):` → `return generate_pack(story_key, skill=skill)`

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_context_pack.py -v` → 全过

- [ ] **Step 5: Commit** — `feat(pack): optional skill hint in pack`

---

## Task 6: pack 完整度检查（标红缺失）

**Files:** Modify `pack.py` `_render_pack`；Test `tests/test_context_pack.py`（追加）

- [ ] **Step 1: 追加测试**

```python
def test_pack_flags_missing_refs(isolated_story_home, tmp_path):
    db.create_story(story_key="S-gap", title="t", workspace=str(tmp_path))
    # 没有 spec/branch/document
    content = generate_pack("S-gap")["content"]
    assert "⚠ 缺 spec" in content
    assert "⚠ 缺 branch" in content


def test_pack_no_gap_flags_when_complete(isolated_story_home, tmp_path):
    db.create_story(story_key="S-ok", title="t", workspace=str(tmp_path))
    db.create_project(name="p", repo_path=str(tmp_path))
    db.bind_story_project("S-ok", 1, branch="feature/x")
    db.create_document("S-ok", kind="spec", ref="spec.md")
    content = generate_pack("S-ok")["content"]
    assert "⚠ 缺" not in content
```

- [ ] **Step 2: 跑失败** — FAIL

- [ ] **Step 3: 实现** — 在 `_render_pack` return 前（parent 节之后）加：

```python
    # 完整度检查
    gaps = []
    has_spec = any(d.get("kind") == "spec" for d in bundle.documents)
    if not has_spec:
        gaps.append("spec")
    if not bundle.story_projects:
        gaps.append("branch")
    if story.get("tapd_type") == "bug":
        if not any(d.get("kind") == "bugfix-report" for d in bundle.documents):
            gaps.append("bugfix-report")
    if gaps:
        lines.append("")
        lines.append("## 完整度")
        for g in gaps:
            lines.append(f"- ⚠ 缺 {g}")
```

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_context_pack.py -v` → 全过

- [ ] **Step 5: Commit** — `feat(pack): completeness check flags missing refs`

---

## Task 7: POST /resolve 端点

**Files:** Modify `api.py`；Test `tests/test_resolve.py`（新建）

- [ ] **Step 1: 写测试** — create `tests/test_resolve.py`:

```python
"""Tests for bug resolve endpoint."""
from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.api import app
from story_lifecycle.db import models as db


def test_resolve_bug_updates_status_and_tapd(monkeypatch, isolated_story_home, tmp_path):
    db.create_story(story_key="BUG-r", title="b", workspace=str(tmp_path),
                    source_type="tapd", source_id="bug_1009779", tapd_type="bug")
    import story_lifecycle.orchestrator.api as api_mod
    updated = {}
    class FakeApi:
        def update_bug(self, bid, fields): updated["bug"] = bid; updated["fields"] = fields; return True
    monkeypatch.setattr(api_mod, "_load_tapd_config", lambda: {"workspace_id": "44381896"})
    monkeypatch.setattr("story_lifecycle.sources.tapd_api.TapdApi", lambda **kw: FakeApi())
    client = TestClient(app)
    r = client.post("/api/story/BUG-r/resolve")
    assert r.status_code == 200
    assert r.json()["has_bugfix_report"] is False
    assert updated["bug"] == "1009779"
    assert updated["fields"] == {"status": "resolved"}
    s = db.get_story("BUG-r")
    assert s["status"] == "completed" and s["tapd_status"] == "resolved"


def test_resolve_404_nonexistent(isolated_story_home):
    assert TestClient(app).post("/api/story/NOPE/resolve").status_code == 404


def test_resolve_400_not_bug(isolated_story_home, tmp_path):
    db.create_story(story_key="REQ-r", title="r", workspace=str(tmp_path), tapd_type="story")
    assert TestClient(app).post("/api/story/REQ-r/resolve").status_code == 400
```

- [ ] **Step 2: 跑失败** — 404

- [ ] **Step 3: 实现** — in `api.py`，在 sync-related-bugs 端点后：

```python
@app.post("/api/story/{bug_key}/resolve")
def api_resolve_bug(bug_key: str):
    """Mark a bug resolved: update TAPD + local status. Warns if no bugfix-report."""
    story = db.get_story(bug_key)
    if not story:
        raise HTTPException(status_code=404, detail=f"story not found: {bug_key}")
    if story.get("tapd_type") != "bug":
        raise HTTPException(status_code=400, detail="not a bug")
    has_evidence = any(
        d.get("kind") == "bugfix-report" for d in db.get_story_documents(bug_key)
    )
    config = _load_tapd_config()
    if config.get("workspace_id") and story.get("source_id"):
        api = TapdApi(workspace_id=config["workspace_id"])
        bug_id = story["source_id"].removeprefix("bug_")
        api.update_bug(bug_id, {"status": "resolved"})
    db.update_story(bug_key, status="completed", tapd_status="resolved")
    return {"ok": True, "has_bugfix_report": has_evidence}
```

- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_resolve.py -v` → 3 passed

- [ ] **Step 5: Commit** — `feat(api): POST /resolve bug endpoint`

---

## Task 8: 前端进 story 详情触发 sync（节流）

**Files:** Modify `frontend/src/pages/StoryDetailPage.tsx`（非 TDD，build 验证）

- [ ] **Step 1: 加 sync 触发** — 在 `StoryDetailPage` 组件内，`detail` query 之后加一个节流 sync query：

```tsx
  // 进详情触发关联 bug 同步（节流 5min，避免每次都打 TAPD）
  useQuery({
    queryKey: ['sync-related-bugs', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/sync-related-bugs`, { method: 'POST' })
      return r.ok ? r.json() : null
    },
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
```

（`useQuery` 已 import；放在 detail query 之后、不影响主渲染）

- [ ] **Step 2: build** — `npm --prefix frontend run build` → 通过

- [ ] **Step 3: Commit** — `feat(frontend): trigger sync-related-bugs on story detail (throttled)` + web 产物

---

## Task 9: 前端 ContextTab — skill 下拉 + 完整度显示

**Files:** Modify `frontend/src/components/ContextTab.tsx`（非 TDD）

- [ ] **Step 1: 加 skill 选择 + 完整度** — `copyPack` 读选中 skill，pack 端点带 `?skill=`。pack 内容里 `⚠ 缺` 自动显示（无需额外解析）。在 toolbar 加 skill 下拉：

```tsx
  const [skill, setSkill] = useState('')

  async function copyPack() {
    const url = skill
      ? `/api/story/${storyKey}/context/pack?skill=${encodeURIComponent(skill)}`
      : `/api/story/${storyKey}/context/pack`
    const r = await fetch(url)
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
```

toolbar JSX 加（在复制按钮旁）：

```tsx
        <select value={skill} onChange={(e) => setSkill(e.target.value)} className="ctx-skill-select">
          <option value="">（中性，不指定 skill）</option>
          <option value="bug-fix">bug-fix</option>
          <option value="env-debug">env-debug</option>
        </select>
```

- [ ] **Step 2: build** — `npm --prefix frontend run build` → 通过

- [ ] **Step 3: Commit** — `feat(frontend): ContextTab skill selector`

---

## Task 10: 前端 bug 详情「标记已修复」按钮

**Files:** Modify `frontend/src/components/StorySidebar.tsx`（非 TDD）—— bug 类型显示 resolve 按钮

- [ ] **Step 1: 读 StorySidebar 现状** 确认 props（storyKey/status 等），判断 tapd_type 是否已传入；若无，从 detail 传。

- [ ] **Step 2: 加 resolve 按钮** — 在 sidebar 底部，当 story 是 bug 类型时显示「标记已修复」按钮，点击 `POST /resolve`：

```tsx
  async function handleResolve() {
    if (!window.confirm('确认 bug 已修复？会更新 TAPD + 本地状态。')) return
    const r = await fetch(`/api/story/${storyKey}/resolve`, { method: 'POST' })
    if (r.ok) {
      const body = await r.json()
      if (!body.has_bugfix_report) alert('⚠ 未发现 bugfix-report 证据，建议补记后再 resolve')
      window.location.reload()
    } else {
      alert('resolve 失败')
    }
  }

  // JSX（bug 类型时）：
  {isBug && <button className="btn btn-primary" onClick={handleResolve}>标记已修复</button>}
```

（`isBug` 从 detail.tapdType === 'bug' 判断；若 StorySidebar 拿不到 tapdType，从 StoryDetailPage 传入）

- [ ] **Step 3: build** — `npm --prefix frontend run build` → 通过

- [ ] **Step 4: 手动验证** — `story serve` → 进 bug 1009779 详情 → 看到「标记已修复」按钮（不实际点，避免改 TAPD）

- [ ] **Step 5: Commit** — `feat(frontend): bug resolve button` + web 产物

---

## 端到端验证（所有 task 后）

`story serve` → 进需求 1065460 详情（触发 sync-related-bugs）→ bug 1009779 进来（parent=1065460）→ 进 bug 1009779 详情 → ContextTab 复制 pack（选 bug-fix skill）→ pack 含「建议调用 /bug-fix」+ bug 元信息 +「关联需求：删除联系人」+ 需求的 spec/branch/DDL + 完整度标红（bugfix-report 缺）。

---

## Self-Review

- **Spec 覆盖**：Part 1（Task 1-3,8）、Part 2（Task 4,5）、Part 3（Task 6）、Part 4（Task 7,10 + bugfix-report 由 document ref + 完整度覆盖，结构化三节属 hc-all skill 范围外）。全覆盖。
- **Anti-tampering**：无 CORE 参数。
- **Placeholder**：无；每步含 exact code。
- **类型一致**：`get_related_bugs`、`parent_key`、`skill`、`resolve` 在测试/实现/前端三处一致；`bugfix-report` 作为 document kind 一致。
- **注意**：前端 Task 8-10 非 TDD（无前端测试框架），用 build + 手动验证；bugfix-report 结构化三节是 hc-all 新 skill 的职责（本 plan 范围外），story-lifecycle 侧只管 document ref + 完整度检查。
