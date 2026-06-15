# Story Context（复制注入 + 回填 skill）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个 story 的关系（分支/PRD/spec/plan/DDL/Nacos/交付）可被一键复制成中性资料包注入任意 AI agent，并提供通用 agent skill 回填这些关系到 DB。

**Architecture:** 复用现有 `ContextResolver` 组装关系。新增 `context/pack.py` 渲染混合浓度中性 markdown（本地文件给路径、Nacos 内联）。新增写关系 API 端点调现有 db 函数（`create_document`/`create_change_item`/`bind_story_project`/`update_story_project`）。前端详情页加 Context Tab + 复制按钮。hc-all 加 `.agents/skills/story-context` 通用 skill 教 agent 回填。

**Tech Stack:** Python 3.10 / FastAPI / SQLite（后端）；React 19 + TypeScript + react-query（前端）；无前端测试框架（前端用 build + 手动验证，非 TDD）。

**Conventions:** 所有 commit 以 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` 结尾。本项目直接提交 main。后端命令在 `D:\story-lifecycle`，前端在 `frontend/`，hc-all 改动在 `D:\hc-all`（非 git）。

---

## File Structure

**后端（`src/story_lifecycle/`）**
- Create: `orchestrator/context/pack.py` — 混合浓度中性 renderer
- Modify: `orchestrator/api.py` — 加 4 个端点（GET pack / POST documents / POST change-items / PUT branch）
-（db 层 `create_document`/`create_change_item`/`bind_story_project`/`update_story_project` 已存在，不改）

**前端（`frontend/src/`）**
- Create: `components/ContextTab.tsx` — Context Tab 组件（概览 + 复制按钮）
- Modify: `pages/StoryDetailPage.tsx` — MODULES 加 context、内容区渲染 ContextTab

**测试（`tests/`）**
- Create: `test_context_pack.py` — pack 渲染单测
- Create: `test_context_write.py` — 写端点单测
- Modify: `conftest.py` — `isolated_story_home` 改 autouse（附录 Task D1）

**hc-all（`D:\hc-all\`，非 git）**
- Create: `.agents/skills/story-context/SKILL.md`
- Delete: `.agents/skills/story-lifecycle/`、`.claude/skills/story-lifecycle/`
- Modify: `.agents/skills/dev-workflow/SKILL.md`、`.claude/skills/dev-workflow/SKILL.md`、`AGENTS.md`

---

## Part A — 复制注入（读）

### Task A1: pack.py renderer（TDD）

**Files:**
- Create: `src/story_lifecycle/orchestrator/context/pack.py`
- Test: `tests/test_context_pack.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_context_pack.py`:

```python
"""Tests for context pack renderer (mixed-density, neutral)."""
import pytest
from story_lifecycle.orchestrator.context.pack import generate_pack
from story_lifecycle.db import models as db


def _seed_story(key="S1", tmp_path=None):
    db.create_story(story_key=key, title="测试需求", workspace=str(tmp_path))


def test_pack_renders_branch_and_local_doc_path(isolated_story_home, tmp_path):
    _seed_story("S1", tmp_path)
    db.create_project(name="p1", repo_path=str(tmp_path))
    db.bind_story_project("S1", 1, branch="feature/S1")
    db.create_document("S1", kind="prd", ref="prd/S1.md", summary="需求摘要")
    content = generate_pack("S1")["content"]
    assert "feature/S1" in content      # 分支
    assert "prd/S1.md" in content       # 本地文档给路径
    assert "测试需求" in content


def test_pack_inlines_nacos_evidence(isolated_story_home, tmp_path):
    _seed_story("S2", tmp_path)
    db.create_change_item(
        "S2", kind="nacos", ref="hc-order.yaml",
        summary="改了超时", evidence_ref="timeout: 30s -> 60s",
    )
    content = generate_pack("S2")["content"]
    assert "timeout: 30s -> 60s" in content   # Nacos 正文内联
    assert "改了超时" in content
    assert "## Nacos" in content


def test_pack_ddl_uses_path_not_inlining(isolated_story_home, tmp_path):
    _seed_story("S3", tmp_path)
    db.create_change_item("S3", kind="ddl", ref="sql/V1__add_col.sql", summary="加列")
    content = generate_pack("S3")["content"]
    assert "sql/V1__add_col.sql" in content
    assert "## DDL" in content


def test_pack_is_neutral_no_instruction(isolated_story_home, tmp_path):
    _seed_story("S4", tmp_path)
    content = generate_pack("S4")["content"]
    assert "请实现" not in content
    assert "请修复" not in content
    assert "请按" not in content


def test_pack_returns_revision(isolated_story_home, tmp_path):
    _seed_story("S5", tmp_path)
    result = generate_pack("S5")
    assert result["revision"] == 0
    assert result["story_key"] == "S5"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_pack.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.context.pack'`

- [ ] **Step 3: 实现 pack.py**

Create `src/story_lifecycle/orchestrator/context/pack.py`:

```python
"""Context Pack — render a neutral, mixed-density markdown for injecting into any AI agent.

Mixed density: local files (PRD/spec/plan/DDL) given as paths the agent reads in its
worktree; non-local content (Nacos config, TAPD summary) inlined.
Neutral: states facts only, never issues "please implement" instructions.
"""

from __future__ import annotations

from .resolver import ContextResolver, ContextBundle


def generate_pack(story_key: str) -> dict:
    """Render a context pack for manual injection into an AI agent session.

    Returns {"content": <markdown>, "revision": N, "story_key": story_key}.
    Raises ValueError if story not found.
    """
    from ...db import models as db

    bundle = ContextResolver().resolve(story_key)
    content = _render_pack(story_key, bundle)
    db.log_event(
        story_key,
        stage=bundle.story.get("current_stage", "") if bundle.story else "",
        event_type="context_pack_generated",
        payload={"revision": bundle.revision},
    )
    return {"content": content, "revision": bundle.revision, "story_key": story_key}


def _render_pack(story_key: str, bundle: ContextBundle) -> str:
    story = bundle.story or {}
    lines: list[str] = []

    lines.append(f"# Story 上下文资料包：{story_key}")
    lines.append("")
    lines.append(f"- 标题：{story.get('title', '')}")
    tapd_url = story.get("tapd_url", "")
    if tapd_url:
        lines.append(f"- TAPD：{tapd_url}")
    lines.append(f"- Profile / Stage：{story.get('profile', '')} / {story.get('current_stage', '')}")
    lines.append(f"- Context Revision：{bundle.revision}")
    lines.append("")

    # 绑定项目与分支
    if bundle.story_projects:
        lines.append("## 绑定项目与分支")
        for sp in bundle.story_projects:
            proj = _find_project(bundle.projects, sp.get("project_id"))
            name = proj.get("name", "") if proj else "(未知项目)"
            lines.append(f"- **{name}**：分支 `{sp.get('branch', '')}`")
            wt = sp.get("worktree_path", "")
            if wt and not str(wt).startswith("_pending"):
                lines.append(f"  - worktree：`{wt}`")
            if sp.get("base_branch"):
                lines.append(f"  - 基线：`{sp.get('base_branch', '')}`")
            if sp.get("summary"):
                lines.append(f"  - 影响摘要：{sp.get('summary', '')}")
        lines.append("")

    # 文档（本地文件，给路径）
    if bundle.documents:
        lines.append("## 文档（在 worktree 内可读）")
        for doc in bundle.documents:
            ref = doc.get("ref", "") or "(无路径)"
            lines.append(f"- **{doc.get('kind', '')}**：{ref}")
            if doc.get("summary"):
                lines.append(f"  - 摘要：{doc.get('summary', '')}")
        lines.append("")

    # 变更项：DDL 给路径，Nacos 内联
    ddl = [ci for ci in bundle.change_items if ci.get("kind") == "ddl"]
    nacos = [ci for ci in bundle.change_items if ci.get("kind") == "nacos"]
    others = [ci for ci in bundle.change_items if ci.get("kind") not in ("ddl", "nacos")]
    if ddl:
        lines.append("## DDL（在 worktree 内可读）")
        for ci in ddl:
            lines.append(f"- {ci.get('ref', '') or '(无路径)'}")
            if ci.get("summary"):
                lines.append(f"  - 摘要：{ci.get('summary', '')}")
        lines.append("")
    if nacos:
        lines.append("## Nacos 配置变更（内联）")
        for ci in nacos:
            lines.append(f"- **{ci.get('ref', '') or '(未命名配置)'}**")
            if ci.get("summary"):
                lines.append(f"  - 变更摘要：{ci.get('summary', '')}")
            if ci.get("evidence_ref"):
                lines.append("  ```")
                lines.append(str(ci.get("evidence_ref", "")))
                lines.append("  ```")
        lines.append("")
    if others:
        lines.append("## 其他变更")
        for ci in others:
            lines.append(f"- **{ci.get('kind', '')}**：{ci.get('ref', '')}")
        lines.append("")

    # 交付产物
    if bundle.delivery_artifacts:
        lines.append("## 交付产物")
        for da in bundle.delivery_artifacts:
            url = da.get("url", "")
            lines.append(f"- **{da.get('kind', '')}**：{url or da.get('external_id', '')}")
            if da.get("target_branch"):
                lines.append(f"  - 目标分支：`{da.get('target_branch', '')}`")
        lines.append("")

    return "\n".join(lines)


def _find_project(projects: list[dict], project_id: int | None) -> dict | None:
    for p in projects:
        if p.get("id") == project_id:
            return p
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_pack.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/context/pack.py tests/test_context_pack.py
git commit -m "feat(context): neutral mixed-density pack renderer"
```
（加 Co-Authored-By trailer）

---

### Task A2: GET /context/pack 端点（TDD）

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py`（在 `api_get_snapshot` 之后，约 line 1423）
- Test: `tests/test_context_pack.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `tests/test_context_pack.py`:

```python
from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.api import app


def test_pack_endpoint_returns_content(isolated_story_home, tmp_path):
    _seed_story("E1", tmp_path)
    client = TestClient(app)
    r = client.get("/api/story/E1/context/pack")
    assert r.status_code == 200
    body = r.json()
    assert "content" in body
    assert "E1" in body["content"]


def test_pack_endpoint_404_unknown_story(isolated_story_home):
    client = TestClient(app)
    r = client.get("/api/story/NOPE/context/pack")
    assert r.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_pack.py::test_pack_endpoint_returns_content -v`
Expected: FAIL — 404（端点不存在，被其他路由捕获或 404）

- [ ] **Step 3: 加端点**

In `api.py`, after `api_get_snapshot` (around line 1422), add:

```python
@app.get("/api/story/{story_key}/context/pack")
def api_get_context_pack(story_key: str):
    """Render a neutral mixed-density context pack for AI injection."""
    try:
        from .context.pack import generate_pack

        return generate_pack(story_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_pack.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py tests/test_context_pack.py
git commit -m "feat(api): GET /context/pack endpoint"
```

---

### Task A3: 前端 ContextTab 组件（非 TDD，build 验证）

**Files:**
- Create: `frontend/src/components/ContextTab.tsx`

- [ ] **Step 1: 写组件**

Create `frontend/src/components/ContextTab.tsx`:

```tsx
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

interface ContextDoc { kind: string; ref: string; summary?: string }
interface ContextChange { kind: string; ref: string; summary?: string; evidence_ref?: string }
interface ContextBundle {
  story: { title?: string; tapd_url?: string; profile?: string; current_stage?: string }
  story_projects: { project_id: number; branch?: string; worktree_path?: string; base_branch?: string; summary?: string }[]
  projects: { id: number; name?: string }[]
  documents: ContextDoc[]
  change_items: ContextChange[]
  delivery_artifacts: { kind?: string; url?: string; target_branch?: string }[]
  revision: number
}

export default function ContextTab({ storyKey }: { storyKey: string }) {
  const [copied, setCopied] = useState(false)

  const { data: ctx } = useQuery<ContextBundle>({
    queryKey: ['context', storyKey],
    queryFn: async () => {
      const r = await fetch(`/api/story/${storyKey}/context`)
      if (!r.ok) throw new Error('load context failed')
      return r.json()
    },
  })

  async function copyPack() {
    const r = await fetch(`/api/story/${storyKey}/context/pack`)
    const body = await r.json()
    await navigator.clipboard.writeText(body.content || '')
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const projName = (pid: number) => ctx?.projects.find((p) => p.id === pid)?.name || '(未知项目)'
  const ddl = (ctx?.change_items || []).filter((c) => c.kind === 'ddl')
  const nacos = (ctx?.change_items || []).filter((c) => c.kind === 'nacos')

  return (
    <div className="context-tab">
      <div className="ctx-toolbar">
        <button className="btn btn-primary" onClick={copyPack}>
          {copied ? '✓ 已复制' : '复制上下文资料包'}
        </button>
        <span className="ctx-hint">粘贴到任意 AI agent 即可（开发/改 bug/排查通用）</span>
      </div>

      <section>
        <h4>绑定项目与分支</h4>
        {(ctx?.story_projects || []).length === 0 && <p className="ctx-empty">未绑定项目</p>}
        {(ctx?.story_projects || []).map((sp) => (
          <div key={sp.project_id} className="ctx-item">
            <strong>{projName(sp.project_id)}</strong>：分支 <code>{sp.branch || '-'}</code>
            {sp.base_branch && <span> （基线 {sp.base_branch}）</span>}
            {sp.summary && <div className="ctx-sub">{sp.summary}</div>}
          </div>
        ))}
      </section>

      <section>
        <h4>文档（{ctx?.documents?.length || 0}）</h4>
        {(ctx?.documents || []).map((d, i) => (
          <div key={i} className="ctx-item">
            <strong>{d.kind}</strong>：{d.ref || '(无路径)'}
            {d.summary && <div className="ctx-sub">{d.summary}</div>}
          </div>
        ))}
      </section>

      <section>
        <h4>DDL（{ddl.length}） · Nacos（{nacos.length}）</h4>
        {ddl.map((c, i) => (
          <div key={`d${i}`} className="ctx-item"><strong>DDL</strong>：{c.ref} {c.summary && <span className="ctx-sub">— {c.summary}</span>}</div>
        ))}
        {nacos.map((c, i) => (
          <div key={`n${i}`} className="ctx-item"><strong>Nacos</strong>：{c.ref} {c.summary && <span className="ctx-sub">— {c.summary}</span>}</div>
        ))}
      </section>

      <section>
        <h4>交付产物（{ctx?.delivery_artifacts?.length || 0}）</h4>
        {(ctx?.delivery_artifacts || []).map((da, i) => (
          <div key={i} className="ctx-item"><strong>{da.kind}</strong>：{da.url} {da.target_branch && <span className="ctx-sub">→ {da.target_branch}</span>}</div>
        ))}
      </section>
    </div>
  )
}
```

- [ ] **Step 2: build 验证类型**

Run: `npm --prefix frontend run build`
Expected: 构建通过（tsc 无类型错误）。ContextTab 此时未被引用，但独立文件应编译通过。

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ContextTab.tsx
git commit -m "feat(frontend): ContextTab component"
```

---

### Task A4: StoryDetailPage 接入 Context Tab

**Files:**
- Modify: `frontend/src/pages/StoryDetailPage.tsx`

- [ ] **Step 1: import ContextTab**

In `StoryDetailPage.tsx`, after the `import TerminalTab` line (line 12), add:

```tsx
import ContextTab from '../components/ContextTab'
```

- [ ] **Step 2: MODULES 加 context**

Replace the `MODULES` array (lines 15-22) to add the context entry before terminal:

```tsx
const MODULES = [
  { id: 'overview', icon: '📊', label: '概览' },
  { id: 'code', icon: '💻', label: '代码变更' },
  { id: 'loop', icon: '🔁', label: '对抗循环' },
  { id: 'test', icon: '🧪', label: '测试' },
  { id: 'quality', icon: '🛡', label: '质量 & Gate' },
  { id: 'context', icon: '📄', label: '上下文' },
  { id: 'terminal', icon: '💻', label: '终端' },
]
```

- [ ] **Step 3: 内容区渲染 ContextTab**

In the content area, before the `{activeTab === 'terminal' && (` block (around line 185), add:

```tsx
          {activeTab === 'context' && <ContextTab storyKey={storyKey} />}
```

- [ ] **Step 4: build 验证**

Run: `npm --prefix frontend run build`
Expected: 构建通过，产物写入 `src/story_lifecycle/web/`

- [ ] **Step 5: 手动验证**

`story serve` → 打开 http://127.0.0.1:8180 → 进任一 story 详情 → 应看到「上下文」Tab，点「复制上下文资料包」复制成功。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/StoryDetailPage.tsx src/story_lifecycle/web
git commit -m "feat(frontend): wire Context tab into story detail"
```

---

## Part B — 回填 skill（写）

### Task B1: POST /context/documents 端点（TDD）

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py`（在 pack 端点后）
- Test: `tests/test_context_write.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_context_write.py`:

```python
"""Tests for context write endpoints (agent backfill)."""
from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.api import app
from story_lifecycle.db import models as db


def _seed(key, tmp_path):
    db.create_story(story_key=key, title="t", workspace=str(tmp_path))


def test_add_document(isolated_story_home, tmp_path):
    _seed("W1", tmp_path)
    client = TestClient(app)
    r = client.post("/api/story/W1/context/documents", json={"kind": "prd", "ref": "prd/W1.md", "summary": "s"})
    assert r.status_code == 200
    assert r.json()["kind"] == "prd"
    docs = db.get_story_documents("W1")
    assert len(docs) == 1 and docs[0]["ref"] == "prd/W1.md"
    assert db.get_context_revision("W1") >= 1   # revision bumped


def test_add_document_404(isolated_story_home):
    client = TestClient(app)
    r = client.post("/api/story/NOPE/context/documents", json={"kind": "prd"})
    assert r.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_write.py::test_add_document -v`
Expected: FAIL — 404（端点不存在）

- [ ] **Step 3: 加端点**

In `api.py`, after `api_get_context_pack`, add:

```python
class AddDocumentRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    project_id: int | None = None


@app.post("/api/story/{story_key}/context/documents")
def api_add_document(story_key: str, req: AddDocumentRequest):
    """Add a document (prd/spec/plan) — agent backfill."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    doc = db.create_document(
        story_key, req.kind, project_id=req.project_id, ref=req.ref,
        summary=req.summary, evidence_ref=req.evidence_ref, source="agent",
    )
    db.bump_context_revision(story_key)
    return doc
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_write.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py tests/test_context_write.py
git commit -m "feat(api): POST /context/documents endpoint"
```

---

### Task B2: POST /context/change-items 端点（TDD）

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py`
- Test: `tests/test_context_write.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `tests/test_context_write.py`:

```python
def test_add_change_item_nacos(isolated_story_home, tmp_path):
    _seed("W2", tmp_path)
    client = TestClient(app)
    r = client.post("/api/story/W2/context/change-items", json={
        "kind": "nacos", "ref": "hc-order.yaml",
        "summary": "改超时", "evidence_ref": "timeout: 30 -> 60",
    })
    assert r.status_code == 200
    cis = db.get_story_change_items("W2")
    assert len(cis) == 1 and cis[0]["evidence_ref"] == "timeout: 30 -> 60"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_write.py::test_add_change_item_nacos -v`
Expected: FAIL

- [ ] **Step 3: 加端点**

In `api.py`, after `api_add_document`, add:

```python
class AddChangeItemRequest(BaseModel):
    kind: str
    ref: str = ""
    summary: str = ""
    evidence_ref: str = ""
    environment: str = ""
    project_id: int | None = None


@app.post("/api/story/{story_key}/context/change-items")
def api_add_change_item(story_key: str, req: AddChangeItemRequest):
    """Add a change item (ddl/nacos) — agent backfill."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    ci = db.create_change_item(
        story_key, req.kind, project_id=req.project_id, ref=req.ref,
        summary=req.summary, evidence_ref=req.evidence_ref,
        environment=req.environment, source="agent",
    )
    db.bump_context_revision(story_key)
    return ci
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_write.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py tests/test_context_write.py
git commit -m "feat(api): POST /context/change-items endpoint"
```

---

### Task B3: PUT /context/branch 端点（TDD）

**Files:**
- Modify: `src/story_lifecycle/orchestrator/api.py`
- Test: `tests/test_context_write.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `tests/test_context_write.py`:

```python
def test_set_branch_creates_binding(isolated_story_home, tmp_path):
    _seed("W3", tmp_path)
    db.create_project(name="p3", repo_path=str(tmp_path))
    client = TestClient(app)
    r = client.put("/api/story/W3/context/branch", json={"project_id": 1, "branch": "feature/W3"})
    assert r.status_code == 200
    assert r.json()["branch"] == "feature/W3"


def test_set_branch_updates_existing(isolated_story_home, tmp_path):
    _seed("W4", tmp_path)
    db.create_project(name="p4", repo_path=str(tmp_path))
    db.bind_story_project("W4", 1, branch="old-branch")
    client = TestClient(app)
    r = client.put("/api/story/W4/context/branch", json={"project_id": 1, "branch": "new-branch"})
    assert r.status_code == 200
    assert r.json()["branch"] == "new-branch"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_write.py::test_set_branch_creates_binding -v`
Expected: FAIL

- [ ] **Step 3: 加端点**

In `api.py`, after `api_add_change_item`, add:

```python
class SetBranchRequest(BaseModel):
    project_id: int
    branch: str
    worktree_path: str = ""
    base_branch: str = "main"


@app.put("/api/story/{story_key}/context/branch")
def api_set_branch(story_key: str, req: SetBranchRequest):
    """Create or update a story-project branch binding — agent backfill."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    existing = db.get_story_project(story_key, req.project_id)
    if existing:
        db.update_story_project(
            story_key, req.project_id,
            branch=req.branch, worktree_path=req.worktree_path, base_branch=req.base_branch,
        )
    else:
        db.bind_story_project(
            story_key, req.project_id,
            branch=req.branch, worktree_path=req.worktree_path, base_branch=req.base_branch,
        )
    db.bump_context_revision(story_key)
    return db.get_story_project(story_key, req.project_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_context_write.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/api.py tests/test_context_write.py
git commit -m "feat(api): PUT /context/branch endpoint"
```

---

### Task B4: hc-all story-context skill 文档

**Files:**
- Create: `D:\hc-all\.agents\skills\story-context\SKILL.md`

- [ ] **Step 1: 写 skill 文档**

Create `D:\hc-all\.agents\skills\story-context\SKILL.md`:

```markdown
---
name: story-context
description: Maintain the context relationships (branch/PRD/spec/plan/DDL/Nacos) for a TAPD story by writing them back to the story-lifecycle DB. Triggers on "维护 story 上下文", "记录分支/PRD/DDL/Nacos", "回填 story 关系", or when finishing a change for a tracked story.
---

# Story Context 维护

把当前 story 的关系（分支、PRD、spec、plan、DDL、Nacos 配置变更）写回 story-lifecycle，
便于后续一键导出"上下文资料包"注入任意 AI agent。

## 前置

story-lifecycle server 在 8180 运行（`story serve`）。API 基址：

```bash
API="http://127.0.0.1:8180/api/story"
```

## 何时做

- 开始开发一个 TAPD story 时：记录分支
- 写完/改完一个 story 时：记录新增的 PRD/spec/plan 路径、DDL、Nacos 配置变更

## 写关系（curl）

### 1. 记录分支（开始开发时）

```bash
# project_id 从 `curl -s http://127.0.0.1:8180/api/projects` 取
curl -s -X PUT "$API/<STORY_KEY>/context/branch" \
  -H "Content-Type: application/json" \
  -d '{"project_id": <ID>, "branch": "feature/zzh/xxx_0615", "base_branch": "main"}'
```

### 2. 记录文档（PRD/spec/plan）—— 给文件路径

```bash
curl -s -X POST "$API/<STORY_KEY>/context/documents" \
  -H "Content-Type: application/json" \
  -d '{"kind": "prd", "ref": "docs/spec/xxx.md", "summary": "一句话摘要"}'
```

`kind` ∈ `prd | spec | plan`。`ref` 用相对仓库的路径（导出资料包时 agent 在 worktree 自己读全文）。

### 3. 记录 DDL —— 给 SQL 文件路径

```bash
curl -s -X POST "$API/<STORY_KEY>/context/change-items" \
  -H "Content-Type: application/json" \
  -d '{"kind": "ddl", "ref": "hc-order/sql/V12__add_col.sql", "summary": "t_order 加 risk_score 列"}'
```

### 4. 记录 Nacos 配置变更 —— 内联正文

Nacos 配置不在本地仓库，**必须把变更内容写进 summary + evidence_ref**（导出资料包时会内联）：

```bash
curl -s -X POST "$API/<STORY_KEY>/context/change-items" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "nacos",
    "ref": "hc-order-dev.yaml#order.timeout",
    "summary": "订单超时 30s -> 60s",
    "evidence_ref": "order.timeout: 30\norder.timeout: 60"
  }'
```

## 约定

- **DDL / 文档**：`ref` 给仓库内文件路径，不内联正文。
- **Nacos**：内容写 `summary`（一句话）+ `evidence_ref`（before/after 正文），因为配置不在 worktree。
- 每次写入自动 bump context revision。
- 写完后，story 详情页「上下文」Tab →「复制上下文资料包」即可导出。

## 验证

写完后查看：

```bash
curl -s "$API/<STORY_KEY>/context" | python -m json.tool
```
```

- [ ] **Step 2: 手动验证**

（需要 server 运行）`story serve` → curl 一个 POST documents → `curl /api/story/<key>/context` 确认写入。

- [ ] **Step 3: Commit（hc-all 非 git，跳过；记录完成）**

hc-all 不是 git 仓库，文件直接落盘即完成。

---

## Part C — 清理（删旧 skill）

### Task C1: 删旧 story-lifecycle skill + 清引用

**Files:**
- Delete: `D:\hc-all\.agents\skills\story-lifecycle\`、`D:\hc-all\.claude\skills\story-lifecycle\`
- Modify: `D:\hc-all\.agents\skills\dev-workflow\SKILL.md`（删第 50-64 "## Story Lifecycle 集成"整节）
- Modify: `D:\hc-all\.claude\skills\dev-workflow\SKILL.md`（同上）
- Modify: `D:\hc-all\AGENTS.md`（删第 57-64 "### Story Lifecycle 集成"整节）

- [ ] **Step 1: 删旧 skill 目录**

```bash
rm -rf /d/hc-all/.agents/skills/story-lifecycle
rm -rf /d/hc-all/.claude/skills/story-lifecycle
```

- [ ] **Step 2: 清 dev-workflow 引用（.agents 版）**

在 `D:\hc-all\.agents\skills\dev-workflow\SKILL.md` 中，删除从 `## Story Lifecycle 集成`（第 50 行）到 `## 不使用的 superpowers 技能`（第 66 行）之前的整节内容（含表格与"注意"列表，即第 50-65 行）。删除后第 66 行 `## 不使用的 superpowers 技能` 紧接上一节。

- [ ] **Step 3: 清 dev-workflow 引用（.claude 版）**

对 `D:\hc-all\.claude\skills\dev-workflow\SKILL.md` 做同样删除（第 50-65 行整节）。

- [ ] **Step 4: 清 AGENTS.md 集成节**

在 `D:\hc-all\AGENTS.md` 中删除 `### Story Lifecycle 集成` 整节（第 57-64 行，从 `### Story Lifecycle 集成` 标题到文件末尾或下一节）。

- [ ] **Step 5: 验证无悬空引用**

```bash
grep -rn "story-lifecycle" /d/hc-all/.agents /d/hc-all/.claude /d/hc-all/AGENTS.md 2>/dev/null | grep -v skill-log.jsonl
```
Expected: 无输出（或仅 skill-log.jsonl 历史日志，可忽略）。如有残留，逐一清理。

- [ ] **Step 6: 完成（hc-all 非 git，无 commit）**

---

## 附录 — 测试隔离修复（解决"测试污染主库"）

### Task D1: isolated_story_home 改 autouse

**根因**：`tests/conftest.py` 的 `isolated_story_home` fixture 非 autouse，漏用它的测试直接写主库。

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: 记录主库当前 story 行数（证明污染存在）**

```bash
python -c "import sqlite3,os; c=sqlite3.connect(f'file:'+os.path.expanduser('~/.story-lifecycle/story.db')+'?mode=ro',uri=True); print('story rows:', c.execute('SELECT COUNT(*) FROM story').fetchone()[0])"
```
记下数字 N0。

- [ ] **Step 2: 跑全量测试**

```bash
python -m pytest -q
```

- [ ] **Step 3: 再看主库行数**

```bash
python -c "import sqlite3,os; c=sqlite3.connect(f'file:'+os.path.expanduser('~/.story-lifecycle/story.db')+'?mode=ro',uri=True); print('story rows:', c.execute('SELECT COUNT(*) FROM story').fetchone()[0])"
```
若数字 > N0 → 确认有测试污染主库。

- [ ] **Step 4: 改 conftest 让 DB 隔离 autouse**

在 `tests/conftest.py` 中，把 `isolated_story_home` 的 DB 隔离部分提为一个 autouse fixture。在现有 `_reset_graph_globals` fixture 之后新增：

```python
@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Auto-redirect the story DB to a per-test tmp dir so no test ever writes the real
    ~/.story-lifecycle/story.db. Tests that need a populated DB can still request
    isolated_story_home (which also inits tables)."""
    from story_lifecycle.db import models as db
    import story_lifecycle.orchestrator.nodes as nodes_mod

    story_home = tmp_path / "story-home"
    story_home.mkdir()
    db_path = story_home / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(nodes_mod, "STORY_HOME", story_home)
    monkeypatch.setenv("STORY_HOME", str(story_home))
    db.init_db()
```

并把原 `isolated_story_home` fixture 中已被 autouse 覆盖的 DB 重定向三行（`monkeypatch.setattr(db, "get_db_path", ...)` / `nodes_mod STORY_HOME` / `setenv STORY_HOME` / `db.init_db()`）删除，只保留 profile 加载 patch 部分。修改后 `isolated_story_home` 变为：

```python
@pytest.fixture
def isolated_story_home(_isolated_db, monkeypatch):
    """Isolated home (DB already redirected by _isolated_db autouse) +
    force package built-in profiles."""
    from story_lifecycle.orchestrator.nodes import profile_loader as _pl
    import story_lifecycle.orchestrator.nodes as nodes_mod

    def _load_builtin_only(name: str) -> dict:
        import importlib.resources as _ir
        try:
            ref = _ir.files("story_lifecycle.profiles").joinpath(f"{name}.yaml")
            return __import__("yaml").safe_load(ref.read_text(encoding="utf-8"))
        except (FileNotFoundError, TypeError):
            pass
        raise FileNotFoundError(f"Profile not found: {name}")

    monkeypatch.setattr(nodes_mod, "load_profile", _load_builtin_only)
    monkeypatch.setattr(_pl, "load_profile", _load_builtin_only)
```

- [ ] **Step 5: 跑全量测试确认无回归**

```bash
python -m pytest -q
```
Expected: 全部通过（数量与改前一致或更多）。若有测试因 autouse 隔离而失败（例如某测试依赖真实 HOME），逐个修正使其用 tmp 隔离。

- [ ] **Step 6: 再看主库行数确认不再污染**

```bash
python -m pytest -q >/dev/null 2>&1
python -c "import sqlite3,os; c=sqlite3.connect(f'file:'+os.path.expanduser('~/.story-lifecycle/story.db')+'?mode=ro',uri=True); print('story rows:', c.execute('SELECT COUNT(*) FROM story').fetchone()[0])"
```
Expected: 数字 == N0（不再增长）。

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py
git commit -m "test: autouse DB isolation — stop tests polluting main story.db"
```

---

## Self-Review 结果

- **Spec 覆盖**：Part A（pack + 端点 + 前端 Tab）→ A1-A4；Part B（写端点 + skill）→ B1-B4；Part C（删旧 + 清引用）→ C1；附录（测试隔离）→ D1。spec 每节都有对应 task。
- **Anti-tampering**：spec 无 CORE 参数，不适用。
- **Placeholder 扫描**：无 TBD/TODO；每步含 exact code 或 exact 命令。
- **类型一致**：端点名（`/context/pack`、`/context/documents`、`/context/change-items`、`/context/branch`）在测试、实现、skill 文档三处一致；db 函数名（`create_document`/`create_change_item`/`bind_story_project`/`update_story_project`）与 models.py 一致。
- **注意**：前端无测试框架，A3/A4 用 build + 手动验证（非 TDD），已在 task 内标注。
