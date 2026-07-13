# Project Intelligence Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement P0 (templates/prompts) and P1 (local bootstrap CLI) of the Project Intelligence Bootstrap feature, enabling `story project init-knowledge` to generate a `.story/knowledge/` knowledge pack via CLI headless execution.

**Architecture:** A new `knowledge` subpackage under `src/story_lifecycle/knowledge/` handles path helpers, directory scaffolding, template loading, artifact validation, and stale detection. A new `project` CLI subgroup exposes `init-knowledge` and `sync-knowledge` commands. The bootstrap command renders a prompt template, launches the AI CLI in headless mode, polls for a done file, and validates the generated artifacts.

**Tech Stack:** Python 3.11+, Click CLI, subprocess (headless CLI execution), YAML/JSON/Markdown file I/O.

**Design spec:** `docs/design-project-intelligence-bootstrap.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/story_lifecycle/knowledge/__init__.py` | Package init |
| Create | `src/story_lifecycle/knowledge/paths.py` | Path helpers for `.story/knowledge/` |
| Create | `src/story_lifecycle/knowledge/scaffold.py` | Directory + .gitignore creation |
| Create | `src/story_lifecycle/knowledge/validator.py` | Artifact validation |
| Create | `src/story_lifecycle/knowledge/stale.py` | Stale detection for sync |
| Create | `src/story_lifecycle/knowledge/search.py` | Minimal structured search |
| Create | `src/story_lifecycle/knowledge/bootstrap.py` | Prompt rendering + headless runner |
| Create | `src/story_lifecycle/knowledge/templates/manifest.yaml` | manifest 模板 |
| Create | `src/story_lifecycle/knowledge/templates/product.yaml` | product 模板 |
| Create | `src/story_lifecycle/knowledge/templates/search-catalog.md` | search-catalog 模板 |
| Create | `src/story_lifecycle/knowledge/templates/graph-schema.json` | graph 节点/关系类型定义 |
| Create | `src/story_lifecycle/knowledge/templates/scenario.md` | 单个 scenario 模板 |
| Create | `src/story_lifecycle/knowledge/templates/index.md` | 单个 index 模板 |
| Create | `prompts/knowledge-bootstrap.md` | Bootstrap prompt 模板 |
| Create | `src/story_lifecycle/cli/project.py` | `story project` CLI subgroup |
| Modify | `src/story_lifecycle/cli/main.py:336-349` | Register project subgroup |
| Create | `tests/test_knowledge.py` | Unit tests for knowledge module |

---

### Task 1: Knowledge path helpers

**Files:**
- Create: `src/story_lifecycle/knowledge/__init__.py`
- Create: `src/story_lifecycle/knowledge/paths.py`
- Test: `tests/test_knowledge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_knowledge.py
from pathlib import Path
from story_lifecycle.knowledge.paths import (
    knowledge_dir,
    manifest_path,
    product_path,
    search_catalog_path,
    graph_dir,
    graph_json_path,
    scenarios_dir,
    indexes_dir,
    index_by_domain_dir,
    playbooks_dir,
    declarations_dir,
    reviews_dir,
    events_dir,
    cache_dir,
    knowledge_done_file,
    knowledge_context_dir,
)


def test_knowledge_dir():
    assert knowledge_dir("/ws") == Path("/ws/.story/knowledge")


def test_manifest_path():
    assert manifest_path("/ws") == Path("/ws/.story/knowledge/manifest.yaml")


def test_product_path():
    assert product_path("/ws") == Path("/ws/.story/knowledge/product.yaml")


def test_search_catalog_path():
    assert search_catalog_path("/ws") == Path("/ws/.story/knowledge/search-catalog.md")


def test_graph_dir():
    assert graph_dir("/ws") == Path("/ws/.story/knowledge/graph")


def test_graph_json_path():
    assert graph_json_path("/ws") == Path("/ws/.story/knowledge/graph/product-context-graph.json")


def test_scenarios_dir():
    assert scenarios_dir("/ws") == Path("/ws/.story/knowledge/scenarios")


def test_indexes_dir():
    assert indexes_dir("/ws") == Path("/ws/.story/knowledge/indexes")


def test_index_by_domain_dir():
    assert index_by_domain_dir("/ws") == Path("/ws/.story/knowledge/indexes/by-domain")


def test_playbooks_dir():
    assert playbooks_dir("/ws") == Path("/ws/.story/knowledge/playbooks")


def test_declarations_dir():
    assert declarations_dir("/ws") == Path("/ws/.story/knowledge/declarations")


def test_reviews_dir():
    assert reviews_dir("/ws") == Path("/ws/.story/knowledge/reviews")


def test_events_dir():
    assert events_dir("/ws") == Path("/ws/.story/knowledge/events")


def test_cache_dir():
    assert cache_dir("/ws") == Path("/ws/.story/knowledge/cache")


def test_knowledge_done_file():
    assert knowledge_done_file("/ws") == Path(
        "/ws/.story/done/PROJECT-KNOWLEDGE-INIT/knowledge_bootstrap.json"
    )


def test_knowledge_context_dir():
    assert knowledge_context_dir("/ws", "STORY-1") == Path(
        "/ws/.story/context/STORY-1/knowledge-context"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_knowledge.py -v`
Expected: FAIL — `ModuleNotFoundError: story_lifecycle.knowledge`

- [ ] **Step 3: Write implementation**

```python
# src/story_lifecycle/knowledge/__init__.py
"""Project Intelligence Bootstrap — local knowledge pack management."""
```

```python
# src/story_lifecycle/knowledge/paths.py
"""Path helpers for .story/knowledge/ layout.

All runtime code must use these helpers instead of hand-building paths.

    .story/knowledge/
      product.yaml
      manifest.yaml
      search-catalog.md
      scenarios/<domain>/
      indexes/*.md
      indexes/by-domain/<domain>.md
      graph/product-context-graph.json
      playbooks/
      declarations/
      reviews/
      events/
      cache/
"""

from __future__ import annotations

from pathlib import Path


def knowledge_dir(workspace: str | Path) -> Path:
    return Path(workspace) / ".story" / "knowledge"


def manifest_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "manifest.yaml"


def product_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "product.yaml"


def search_catalog_path(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "search-catalog.md"


def graph_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "graph"


def graph_json_path(workspace: str | Path) -> Path:
    return graph_dir(workspace) / "product-context-graph.json"


def scenarios_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "scenarios"


def indexes_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "indexes"


def index_by_domain_dir(workspace: str | Path) -> Path:
    return indexes_dir(workspace) / "by-domain"


def playbooks_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "playbooks"


def declarations_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "declarations"


def reviews_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "reviews"


def events_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "events"


def cache_dir(workspace: str | Path) -> Path:
    return knowledge_dir(workspace) / "cache"


def knowledge_done_file(workspace: str | Path) -> Path:
    """Done file for the PROJECT-KNOWLEDGE-INIT bootstrap."""
    return Path(workspace) / ".story" / "done" / "PROJECT-KNOWLEDGE-INIT" / "knowledge_bootstrap.json"


def knowledge_context_dir(workspace: str | Path, story_key: str) -> Path:
    return Path(workspace) / ".story" / "context" / story_key / "knowledge-context"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_knowledge.py -v`
Expected: PASS (all 16 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/__init__.py src/story_lifecycle/knowledge/paths.py tests/test_knowledge.py
git commit -m "feat(knowledge): add path helpers for .story/knowledge/ layout"
```

---

### Task 2: Directory scaffold + .gitignore

**Files:**
- Create: `src/story_lifecycle/knowledge/scaffold.py`
- Modify: `tests/test_knowledge.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge.py`:

```python
from story_lifecycle.knowledge.scaffold import scaffold_knowledge_dir


class TestScaffold:
    def test_creates_all_dirs(self, tmp_path):
        scaffold_knowledge_dir(tmp_path)
        dirs = [
            "knowledge/scenarios",
            "knowledge/indexes/by-domain",
            "knowledge/graph",
            "knowledge/playbooks",
            "knowledge/declarations",
            "knowledge/reviews",
            "knowledge/events",
            "knowledge/cache",
            "done/PROJECT-KNOWLEDGE-INIT",
        ]
        for d in dirs:
            assert (tmp_path / ".story" / d).is_dir(), f"Missing .story/{d}"

    def test_creates_gitignore(self, tmp_path):
        scaffold_knowledge_dir(tmp_path)
        gi = tmp_path / ".story" / "knowledge" / ".gitignore"
        assert gi.exists()
        content = gi.read_text(encoding="utf-8")
        assert "/indexes/" in content
        assert "/graph/" in content
        assert "/events/" in content
        assert "/cache/" in content
        assert "product.yaml" not in content

    def test_idempotent(self, tmp_path):
        scaffold_knowledge_dir(tmp_path)
        scaffold_knowledge_dir(tmp_path)  # should not raise
        assert (tmp_path / ".story" / "knowledge").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_knowledge.py::TestScaffold -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write implementation**

```python
# src/story_lifecycle/knowledge/scaffold.py
"""Create .story/knowledge/ directory structure with .gitignore."""

from __future__ import annotations

from pathlib import Path

from .paths import (
    cache_dir,
    declarations_dir,
    events_dir,
    graph_dir,
    index_by_domain_dir,
    indexes_dir,
    knowledge_dir,
    playbooks_dir,
    reviews_dir,
    scenarios_dir,
)

_SUBDIRS = [
    scenarios_dir,
    indexes_dir,
    index_by_domain_dir,
    graph_dir,
    playbooks_dir,
    declarations_dir,
    reviews_dir,
    events_dir,
    cache_dir,
]

_GITIGNORE = """\
/indexes/
/graph/
/events/
/cache/
/reviews/pending-review-items.md
"""


def scaffold_knowledge_dir(workspace: str | Path) -> Path:
    """Create .story/knowledge/ with all subdirs and .gitignore. Idempotent."""
    root = knowledge_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)

    for fn in _SUBDIRS:
        fn(workspace).mkdir(parents=True, exist_ok=True)

    # done dir for bootstrap handshake
    done = Path(workspace) / ".story" / "done" / "PROJECT-KNOWLEDGE-INIT"
    done.mkdir(parents=True, exist_ok=True)

    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE, encoding="utf-8")

    return root
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_knowledge.py::TestScaffold -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/scaffold.py tests/test_knowledge.py
git commit -m "feat(knowledge): add directory scaffold with .gitignore"
```

---

### Task 3: Template files

**Files:**
- Create: `src/story_lifecycle/knowledge/templates/manifest.yaml`
- Create: `src/story_lifecycle/knowledge/templates/product.yaml`
- Create: `src/story_lifecycle/knowledge/templates/search-catalog.md`
- Create: `src/story_lifecycle/knowledge/templates/graph-schema.json`
- Create: `src/story_lifecycle/knowledge/templates/scenario.md`
- Create: `src/story_lifecycle/knowledge/templates/index.md`
- Modify: `tests/test_knowledge.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge.py`:

```python
from story_lifecycle.knowledge.templates import load_template


class TestTemplates:
    @pytest.mark.parametrize("name", [
        "manifest.yaml",
        "product.yaml",
        "search-catalog.md",
        "graph-schema.json",
        "scenario.md",
        "index.md",
    ])
    def test_template_exists_and_nonempty(self, name):
        content = load_template(name)
        assert len(content) > 50, f"{name} is too short"

    def test_manifest_is_valid_yaml(self):
        import yaml
        content = load_template("manifest.yaml")
        data = yaml.safe_load(content)
        assert "version" in data
        assert "product" in data

    def test_graph_schema_is_valid_json(self):
        import json
        content = load_template("graph-schema.json")
        data = json.loads(content)
        assert "node_types" in data
        assert "relation_types" in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_knowledge.py::TestTemplates -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write implementation**

```python
# src/story_lifecycle/knowledge/templates.py
"""Load knowledge template files from package resources."""

from __future__ import annotations

import importlib.resources as _ir


def load_template(name: str) -> str:
    """Load a template file by name from the templates/ directory."""
    ref = _ir.files("story_lifecycle.knowledge.templates").joinpath(name)
    return ref.read_text(encoding="utf-8")
```

```yaml
# src/story_lifecycle/knowledge/templates/manifest.yaml
# Knowledge Pack Manifest — generated by story project init-knowledge
version: 1
product:
  name: ""           # 产品名称，例如 "HappyCash"
  description: ""    # 一句话描述

source:
  commit: ""         # 生成时的 Git commit hash
  timestamp: ""      # ISO 8601 生成时间
  dirty: false       # 是否有未提交变更

status: initializing  # initializing | ready | stale | error

domains: []          # 业务域列表，例如 ["order", "payment", "user"]

artifacts:
  scenarios: []
  indexes: []
  graph: ""
  search_catalog: ""

scan_profile: ""     # 使用的扫描 profile，例如 "java-spring-microservice"

stats:
  services: 0
  apis: 0
  tables: 0
  mq_topics: 0
  scenarios: 0
  bugs_indexed: 0
  test_cases: 0
```

```yaml
# src/story_lifecycle/knowledge/templates/product.yaml
# Product Overview — 项目产品概述
product:
  name: ""
  description: ""
  domains: []

tech_stack:
  backend: []       # 例如 ["Java 8", "Spring Boot", "MyBatis"]
  frontend: []      # 例如 ["React", "Umi", "TypeScript"]
  infrastructure: [] # 例如 ["MySQL", "Redis", "RabbitMQ"]

repositories: []
  # - name: order-service
  #   path: /path/to/repo (本地) 或 git url
  #   description: 订单服务

key_flows: []
  # - name: 用户注册
  #   description: 新用户注册并完成授信
  #   critical: true
```

```markdown
<!-- src/story_lifecycle/knowledge/templates/search-catalog.md -->
# Search Catalog — 知识包检索目录

本文件是 AI 检索知识包的入口目录。每个条目包含关键词、类型和文件路径。

## 业务域

<!-- 按以下格式添加每个业务域 -->
<!-- | 关键词 | 类型 | 文件路径 | 说明 | -->
<!-- |--------|------|----------|------| -->
<!-- | 订单, order | domain | scenarios/order/ | 订单生命周期 | -->

## 场景

<!-- | 关键词 | 类型 | 文件路径 | 说明 | -->
<!-- |--------|------|----------|------| -->
<!-- | 提现, withdraw | scenario | scenarios/order/withdraw.md | 用户发起提现到到账全流程 | -->

## 索引

<!-- | 关键词 | 类型 | 文件路径 | 说明 | -->
<!-- |--------|------|----------|------| -->
<!-- | API, 接口 | api | indexes/api-index.md | 全部 HTTP API 清单 | -->
<!-- | 表, table | table | indexes/table-index.md | 全部数据库表清单 | -->

## 图

<!-- | 关键词 | 类型 | 文件路径 | 说明 | -->
<!-- |--------|------|----------|------| -->
<!-- | 关系, graph | graph | graph/product-context-graph.json | 实体关系图 | -->
```

```json
{
  "node_types": [
    "Product", "Domain", "Scenario", "Repository", "Service",
    "Api", "Feign", "Table", "Field", "MqMessage",
    "StateMachine", "State", "Bug", "TestCase", "Playbook",
    "CodeSymbol", "Doc"
  ],
  "relation_types": [
    "HAS_DOMAIN", "HAS_SCENARIO", "USES_SERVICE", "EXPOSES_API",
    "CALLS_FEIGN", "READS_TABLE", "WRITES_TABLE", "HAS_FIELD",
    "PUBLISHES_MQ", "CONSUMES_MQ", "HAS_STATE_MACHINE", "HAS_STATE",
    "AFFECTS_SCENARIO", "COVERS_SCENARIO", "GUARDS_BUG",
    "DESCRIBES", "SOURCE_REF"
  ],
  "nodes": [],
  "edges": [],
  "_schema_version": 1
}
```

```markdown
<!-- src/story_lifecycle/knowledge/templates/scenario.md -->
# {scenario_name}

## 概述

<!-- 一段话描述这个场景的业务目的和主要流程 -->

## 参与服务

<!-- | 服务 | 角色 | 说明 | -->
<!-- |------|------|------| -->

## 主流程

<!-- 1. 步骤一 -->
<!-- 2. 步骤二 -->

## 涉及接口

<!-- | API | 方法 | 说明 | -->
<!-- |-----|------|------| -->

## 涉及数据表

<!-- | 表名 | 操作 | 说明 | -->
<!-- |------|------|------| -->

## 涉及 MQ

<!-- | Topic | Tag | 角色 | 说明 | -->
<!-- |-------|-----|------|------| -->

## 状态机

<!-- 如果有状态流转，描述状态机 -->

## 已知风险

<!-- | 风险 | 说明 | source_refs | -->
<!-- |------|------|-------------| -->

## source_refs

<!-- - path/to/file.java:L42 -->
<!-- - path/to/config.yaml -->
```

```markdown
<!-- src/story_lifecycle/knowledge/templates/index.md -->
# {index_name}

> 状态: proposed | extracted | verified
> 更新时间: {timestamp}

<!-- 按以下格式添加条目 -->
<!-- 每个条目必须包含 source_refs -->

## 条目

<!-- ### {条目标识} -->
<!-- - 名称: -->
<!-- - 类型: -->
<!-- - 路径: -->
<!-- - 说明: -->
<!-- - 关联业务域: -->
<!-- - source_refs: -->
<!--   - path/to/file -->
<!-- - 状态: proposed | extracted | verified -->
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_knowledge.py::TestTemplates -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/templates/ src/story_lifecycle/knowledge/templates.py tests/test_knowledge.py
git commit -m "feat(knowledge): add template files for manifest, product, graph schema, scenario, index"
```

---

### Task 4: Bootstrap prompt template

**Files:**
- Create: `prompts/knowledge-bootstrap.md`

This is the prompt injected into the AI CLI during headless bootstrap. It instructs the AI to explore the codebase and generate the knowledge pack.

- [ ] **Step 1: Create the prompt template**

```markdown
<!-- prompts/knowledge-bootstrap.md -->
你是项目知识包生成助手。你的任务是探索当前项目的代码库和文档，生成项目知识包。

## 项目信息

- 工作区: {workspace}
- Git commit: {git_commit}
- 扫描 profile: {scan_profile}

## 目标

在 `.story/knowledge/` 目录下生成以下文件：

### 必须生成

1. **manifest.yaml** — 知识包清单，记录版本、来源 commit、业务域列表、产物列表、统计信息
2. **product.yaml** — 产品概述，包括名称、描述、技术栈、仓库列表、关键业务流程
3. **search-catalog.md** — 检索目录，按业务域/场景/索引/图分类列出关键词和文件路径
4. **graph/product-context-graph.json** — 轻量关系图，节点和边按以下 schema：

```json
{graph_schema}
```

### 按需生成（发现即记录）

5. **scenarios/<domain>/<scenario>.md** — 业务场景文档，每个场景包含：概述、参与服务、主流程、涉及接口/表/MQ、状态机、已知风险、source_refs
6. **indexes/service-index.md** — 服务索引
7. **indexes/api-index.md** — HTTP API 索引
8. **indexes/table-index.md** — 数据库表索引
9. **indexes/field-index.md** — 关键字段索引
10. **indexes/mq-index.md** — MQ 消息索引
11. **indexes/state-machine-index.md** — 状态机索引
12. **indexes/enum-index.md** — 枚举/常量索引
13. **indexes/by-domain/<domain>.md** — 每个业务域的聚合视图

## 扫描策略

根据 scan_profile 选择扫描深度：

### java-spring-microservice

- 服务目录结构
- Controller / @RequestMapping / @PostMapping 等注解
- FeignClient 接口定义
- Entity / DTO / VO 类
- MyBatis Mapper XML
- SQL 迁移文件
- RocketMQ / Kafka producer 和 consumer
- Enum 和状态常量
- application.yml 配置

### frontend-react-umi

- 路由配置 (routes)
- 页面组件
- API service 调用
- TypeScript 类型定义
- 权限点
- 用户入口

### python-service

- FastAPI / Flask 路由
- CLI 入口和脚本
- SQL / ORM 模型
- 配置文件
- 定时任务
- MCP tools

## 状态标记规则

所有生成内容必须标记状态：
- `extracted` — 直接从代码/文件中抽取的事实
- `proposed` — AI 根据证据推断的语义内容
- `verified` — 仅用于已有声明文件中的内容

## source_refs 规则

每个关键结论必须附带 source_refs：
```
- path/to/file.java:L42
- path/to/config.yaml:数据库连接配置
```

没有证据的内容标记为 `proposed`，不确定的内容写入 `reviews/pending-review-items.md`。

## 生成规则

1. 先识别产品名称、业务域、技术栈
2. 按业务域逐个扫描场景
3. 每个场景至少关联一个 service、api 或 table
4. 图中节点只存摘要和 source_refs，详细内容留在 Markdown 中
5. 全局索引条目必须在至少一个 by-domain 文件中引用
6. 不确定的内容宁可标记 proposed，不要编造

## 完成后

将结果写入 `.story/done/PROJECT-KNOWLEDGE-INIT/knowledge_bootstrap.json`：

```json
{
  "knowledge_manifest": ".story/knowledge/manifest.yaml",
  "scenario_docs": [".story/knowledge/scenarios/<domain>/<scenario>.md"],
  "index_docs": [".story/knowledge/indexes/<name>-index.md"],
  "graph_json": ".story/knowledge/graph/product-context-graph.json",
  "search_catalog": ".story/knowledge/search-catalog.md",
  "pending_review": ".story/knowledge/reviews/pending-review-items.md",
  "summary": "一句话总结"
}
```

> CRITICAL: The file must contain ONLY raw JSON. No markdown code blocks, no explanations. Pure JSON only — otherwise the system fails.

## 边界

- 只做知识包生成，不修改任何业务代码
- 不安装依赖
- 只使用只读工具（Read, Glob, Grep, Bash for read-only commands）
- 写完 done JSON 就停止
```

- [ ] **Step 2: Commit**

```bash
git add prompts/knowledge-bootstrap.md
git commit -m "feat(knowledge): add bootstrap prompt template for knowledge generation"
```

---

### Task 5: Bootstrap runner — prompt rendering + headless execution

**Files:**
- Create: `src/story_lifecycle/knowledge/bootstrap.py`
- Modify: `tests/test_knowledge.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge.py`:

```python
import json
import yaml
from story_lifecycle.knowledge.bootstrap import render_bootstrap_prompt


class TestBootstrapPrompt:
    def test_render_contains_workspace(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        assert str(tmp_path) in prompt

    def test_render_contains_graph_schema(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        assert "node_types" in prompt
        assert "HAS_DOMAIN" in prompt

    def test_render_default_scan_profile(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        assert "java-spring-microservice" in prompt

    def test_render_custom_scan_profile(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path), scan_profile="python-service")
        assert "python-service" in prompt

    def test_render_reads_git_commit(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        # Should contain either a commit hash or "unknown"
        assert "git_commit" in prompt or "unknown" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_knowledge.py::TestBootstrapPrompt -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write implementation**

```python
# src/story_lifecycle/knowledge/bootstrap.py
"""Render bootstrap prompt and run CLI headless for knowledge generation."""

from __future__ import annotations

import subprocess
import json
import time
from pathlib import Path

from .templates import load_template
from .paths import knowledge_done_file


def _get_git_commit(workspace: str | Path) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(workspace), timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _is_git_dirty(workspace: str | Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(workspace), timeout=10,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def render_bootstrap_prompt(
    workspace: str | Path,
    scan_profile: str = "java-spring-microservice",
) -> str:
    """Render the knowledge bootstrap prompt with project context."""
    template = _load_prompt_template()
    graph_schema = load_template("graph-schema.json")

    return template.format(
        workspace=str(workspace),
        git_commit=_get_git_commit(workspace),
        scan_profile=scan_profile,
        graph_schema=graph_schema,
    )


def _load_prompt_template() -> str:
    """Load the bootstrap prompt template, checking project-local first."""
    # Project-local override
    local = Path.cwd() / ".story" / "prompts" / "knowledge-bootstrap.md"
    if local.exists():
        return local.read_text(encoding="utf-8")

    # Package built-in
    import importlib.resources as _ir
    ref = _ir.files("story_lifecycle").joinpath("prompts").joinpath("knowledge-bootstrap.md")
    try:
        return ref.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        pass

    # Fallback: prompts/ relative to package source
    from ..orchestrator.nodes.profile_loader import _package_dir
    path = _package_dir() / "prompts" / "knowledge-bootstrap.md"
    if path.exists():
        return path.read_text(encoding="utf-8")

    raise FileNotFoundError("knowledge-bootstrap.md prompt template not found")


def run_bootstrap(
    workspace: str | Path,
    scan_profile: str = "java-spring-microservice",
    adapter_name: str = "claude",
    timeout: int = 1800,
) -> dict:
    """Run knowledge bootstrap via CLI headless.

    1. Render prompt
    2. Launch AI CLI in headless mode
    3. Wait for done file (up to timeout seconds)
    4. Return parsed done JSON

    Raises TimeoutError if done file not created within timeout.
    Raises FileNotFoundError if artifacts are missing.
    """
    from ..adapters import get_adapter

    workspace = Path(workspace)
    prompt = render_bootstrap_prompt(workspace, scan_profile)

    adapter = get_adapter(adapter_name)
    cmd = adapter.headless_launch_cmd(model="sonnet", prompt=prompt)
    if cmd is None:
        raise RuntimeError(f"Adapter '{adapter_name}' does not support headless mode")

    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(workspace),
        timeout=timeout,
    )

    done = knowledge_done_file(workspace)
    if done.exists():
        return _parse_done(done)

    # CLI headless may write done file via the AI's Write tool
    # If not found, check if the process output contains JSON
    from ..orchestrator.nodes.robust_json import robust_json_parse
    parsed = robust_json_parse(proc.stdout)
    if parsed:
        return parsed

    raise FileNotFoundError(
        f"Bootstrap done file not found at {done}. "
        f"CLI exit code: {proc.returncode}. "
        f"stdout (first 500 chars): {proc.stdout[:500]}"
    )


def _parse_done(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    from ..orchestrator.nodes.robust_json import robust_json_parse
    parsed = robust_json_parse(raw)
    if not parsed:
        raise ValueError(f"Cannot parse done file: {path}")
    return parsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_knowledge.py::TestBootstrapPrompt -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/bootstrap.py tests/test_knowledge.py
git commit -m "feat(knowledge): add bootstrap runner with prompt rendering and headless execution"
```

---

### Task 6: 知识包产物校验器

**Files:**
- Create: `src/story_lifecycle/knowledge/validator.py`
- Modify: `tests/test_knowledge.py`

校验 `init-knowledge` 生成的知识包是否符合最低标准。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_knowledge.py`：

```python
from story_lifecycle.knowledge.validator import validate_knowledge_pack


class TestValidator:
    def _make_pack(self, tmp_path, *, missing=None, empty_graph=False):
        """辅助方法：创建一个最小合法知识包。"""
        from story_lifecycle.knowledge.scaffold import scaffold_knowledge_dir
        from story_lifecycle.knowledge import paths as kp
        scaffold_knowledge_dir(tmp_path)

        manifest = {
            "version": 1,
            "product": {"name": "Test", "description": "test"},
            "status": "ready",
            "domains": ["order"],
        }
        if missing != "manifest":
            kp.manifest_path(tmp_path).write_text(
                yaml.dump(manifest), encoding="utf-8"
            )

        product = {"product": {"name": "Test"}}
        if missing != "product":
            kp.product_path(tmp_path).write_text(
                yaml.dump(product), encoding="utf-8"
            )

        catalog = "# Search Catalog\n"
        if missing != "search_catalog":
            kp.search_catalog_path(tmp_path).write_text(catalog, encoding="utf-8")

        graph_data = {"node_types": [], "relation_types": [], "nodes": [], "edges": []}
        if empty_graph:
            graph_data = {}
        if missing != "graph":
            kp.graph_json_path(tmp_path).write_text(
                json.dumps(graph_data), encoding="utf-8"
            )

    def test_valid_pack_passes(self, tmp_path):
        self._make_pack(tmp_path)
        errors = validate_knowledge_pack(tmp_path)
        assert errors == []

    def test_missing_manifest(self, tmp_path):
        self._make_pack(tmp_path, missing="manifest")
        errors = validate_knowledge_pack(tmp_path)
        assert any("manifest" in e for e in errors)

    def test_missing_product(self, tmp_path):
        self._make_pack(tmp_path, missing="product")
        errors = validate_knowledge_pack(tmp_path)
        assert any("product" in e for e in errors)

    def test_missing_graph(self, tmp_path):
        self._make_pack(tmp_path, missing="graph")
        errors = validate_knowledge_pack(tmp_path)
        assert any("graph" in e for e in errors)

    def test_empty_graph_passes(self, tmp_path):
        """空图（无节点无边）是合法的 — 初始化时可能还没有数据。"""
        self._make_pack(tmp_path, empty_graph=True)
        errors = validate_knowledge_pack(tmp_path)
        # empty_graph 有 nodes/edges 字段，应通过
        assert not any("graph" in e for e in errors)

    def test_missing_search_catalog(self, tmp_path):
        self._make_pack(tmp_path, missing="search_catalog")
        errors = validate_knowledge_pack(tmp_path)
        assert any("search-catalog" in e for e in errors)

    def test_invalid_graph_json(self, tmp_path):
        self._make_pack(tmp_path)
        from story_lifecycle.knowledge import paths as kp
        kp.graph_json_path(tmp_path).write_text("not json{{{", encoding="utf-8")
        errors = validate_knowledge_pack(tmp_path)
        assert any("graph" in e.lower() for e in errors)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_knowledge.py::TestValidator -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 写实现**

```python
# src/story_lifecycle/knowledge/validator.py
"""校验 .story/knowledge/ 产物是否符合最低标准。"""

from __future__ import annotations

import json
from pathlib import Path

from .paths import (
    manifest_path,
    product_path,
    search_catalog_path,
    graph_json_path,
    scenarios_dir,
    indexes_dir,
)


def validate_knowledge_pack(workspace: str | Path) -> list[str]:
    """返回错误列表，空列表 = 通过。"""
    errors: list[str] = []

    # manifest.yaml 必须存在且可解析
    mp = manifest_path(workspace)
    if not mp.exists():
        errors.append("manifest.yaml 不存在")
    else:
        try:
            import yaml
            data = yaml.safe_load(mp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append("manifest.yaml 格式错误：不是 YAML dict")
        except Exception as e:
            errors.append(f"manifest.yaml 解析失败: {e}")

    # product.yaml 必须存在
    pp = product_path(workspace)
    if not pp.exists():
        errors.append("product.yaml 不存在")

    # search-catalog.md 必须存在
    sc = search_catalog_path(workspace)
    if not sc.exists():
        errors.append("search-catalog.md 不存在")

    # graph JSON 必须存在且可解析
    gp = graph_json_path(workspace)
    if not gp.exists():
        errors.append("graph/product-context-graph.json 不存在")
    else:
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append("graph JSON 格式错误：不是 JSON object")
        except json.JSONDecodeError as e:
            errors.append(f"graph JSON 解析失败: {e}")

    # 至少一个 scenario 目录（警告，不阻塞）
    sd = scenarios_dir(workspace)
    if sd.exists():
        domains = [d for d in sd.iterdir() if d.is_dir()]
        if not domains:
            errors.append("警告：scenarios/ 下没有业务域目录")

    # 至少一个 index 文件（警告）
    idx = indexes_dir(workspace)
    if idx.exists():
        md_files = list(idx.glob("*.md"))
        if not md_files:
            errors.append("警告：indexes/ 下没有索引文件")

    return errors
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_knowledge.py::TestValidator -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/validator.py tests/test_knowledge.py
git commit -m "feat(knowledge): add knowledge pack artifact validator"
```

---

### Task 7: CLI 命令 `story project init-knowledge`

**Files:**
- Create: `src/story_lifecycle/cli/project.py`
- Modify: `src/story_lifecycle/cli/main.py:336-349`
- Modify: `tests/test_knowledge.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_knowledge.py`：

```python
from click.testing import CliRunner
from story_lifecycle.cli.main import cli


class TestProjectCLI:
    def test_project_group_registered(self):
        result = CliRunner().invoke(cli, ["project", "--help"])
        assert result.exit_code == 0
        assert "init-knowledge" in result.output

    def test_init_knowledge_help(self):
        result = CliRunner().invoke(cli, ["project", "init-knowledge", "--help"])
        assert result.exit_code == 0
        assert "scan-profile" in result.output or "scan_profile" in result.output

    def test_init_knowledge_creates_dirs(self, tmp_path, monkeypatch):
        """init-knowledge 至少应创建目录结构（不实际跑 AI CLI）。"""
        monkeypatch.setattr(
            "story_lifecycle.knowledge.bootstrap.run_bootstrap",
            lambda *a, **kw: {"summary": "mocked"},
        )
        result = CliRunner().invoke(
            cli, ["project", "init-knowledge", "-w", str(tmp_path)]
        )
        assert (tmp_path / ".story" / "knowledge").is_dir()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_knowledge.py::TestProjectCLI -v`
Expected: FAIL — 命令未注册

- [ ] **Step 3: 写实现**

```python
# src/story_lifecycle/cli/project.py
"""story project — 项目级知识包管理命令。"""

import click
from pathlib import Path
from rich.console import Console

console = Console()


@click.group()
def project():
    """项目知识包管理。"""
    pass


@project.command("init-knowledge")
@click.option(
    "-w", "--workspace",
    default=None,
    help="工作区目录（默认当前目录）",
)
@click.option(
    "--scan-profile",
    default="java-spring-microservice",
    help="扫描 profile: java-spring-microservice | frontend-react-umi | python-service",
)
@click.option(
    "--adapter",
    default="claude",
    help="AI CLI adapter（默认 claude）",
)
@click.option(
    "--timeout",
    default=1800,
    type=int,
    help="超时秒数（默认 1800 = 30 分钟）",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="只创建目录结构，不执行 AI CLI",
)
def init_knowledge(workspace, scan_profile, adapter, timeout, dry_run):
    """初始化项目知识包。

    扫描项目代码库，生成 .story/knowledge/ 下的知识文件，
    包括 manifest、product、search-catalog、graph、scenarios、indexes。
    """
    from ..knowledge.scaffold import scaffold_knowledge_dir
    from ..knowledge.bootstrap import render_bootstrap_prompt, run_bootstrap
    from ..knowledge.validator import validate_knowledge_pack

    ws = Path(workspace or Path.cwd()).resolve()
    console.print(f"\n[bold cyan]初始化项目知识包[/]")
    console.print(f"  工作区: [dim]{ws}[/]")
    console.print(f"  扫描 profile: [dim]{scan_profile}[/]")

    # 检查是否已存在
    from ..knowledge.paths import manifest_path
    if manifest_path(ws).exists():
        if not click.confirm("知识包已存在，是否覆盖？"):
            console.print("[yellow]已取消。[/]")
            return

    # 创建目录结构
    console.print("\n[1/4] 创建目录结构...")
    scaffold_knowledge_dir(ws)
    console.print("  [green]done[/]")

    if dry_run:
        console.print("\n[dim]--dry-run 模式，不执行 AI CLI。目录已创建。[/]")
        return

    # 渲染 prompt（可选预览）
    console.print("\n[2/4] 渲染 bootstrap prompt...")
    prompt = render_bootstrap_prompt(ws, scan_profile=scan_profile)
    console.print(f"  prompt 长度: [dim]{len(prompt)} 字符[/]")

    # 执行 AI CLI headless
    console.print(f"\n[3/4] 执行 {adapter} CLI (headless)...")
    console.print("[dim]等待 AI 生成知识包（可能需要几分钟）...[/]")
    try:
        result = run_bootstrap(
            ws,
            scan_profile=scan_profile,
            adapter_name=adapter,
            timeout=timeout,
        )
        console.print(f"  [green]AI CLI 完成[/]")
        if result.get("summary"):
            console.print(f"  摘要: {result['summary']}")
    except FileNotFoundError as e:
        console.print(f"\n[red]生成失败: {e}[/]")
        console.print("[dim]请检查 AI CLI 输出或手动重试。[/]")
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        console.print(f"\n[red]超时（{timeout}秒）[/]")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"\n[red]执行出错: {e}[/]")
        raise SystemExit(1)

    # 校验产物
    console.print("\n[4/4] 校验知识包产物...")
    errors = validate_knowledge_pack(ws)
    if errors:
        console.print(f"  [yellow]{len(errors)} 个问题:[/]")
        for e in errors:
            console.print(f"    - {e}")
    else:
        console.print("  [green]所有关键产物校验通过[/]")

    console.print(f"\n[green]知识包初始化完成[/]")
    console.print(f"  位置: [dim]{ws / '.story' / 'knowledge'}[/]")


@project.command("sync-knowledge")
@click.option(
    "-w", "--workspace",
    default=None,
    help="工作区目录（默认当前目录）",
)
def sync_knowledge(workspace):
    """检测知识包是否过期，提示增量更新。

    读取 manifest 中的 source commit，与当前 Git HEAD 对比。
    如果 commit 变化或关键源文件修改时间晚于生成时间，标记为 stale。
    """
    from ..knowledge.stale import check_stale
    from ..knowledge.paths import manifest_path

    ws = Path(workspace or Path.cwd()).resolve()
    mp = manifest_path(ws)

    if not mp.exists():
        console.print("[yellow]知识包不存在。请先运行 story project init-knowledge[/]")
        raise SystemExit(1)

    console.print(f"\n[bold cyan]检测知识包状态[/]")
    console.print(f"  工作区: [dim]{ws}[/]")

    result = check_stale(ws)
    if result["stale"]:
        console.print(f"\n[yellow]知识包已过期[/]")
        console.print(f"  原因: {result['reason']}")
        console.print(f"\n建议运行: [bold]story project init-knowledge -w {ws}[/]")
    else:
        console.print(f"\n[green]知识包是最新的[/]")
        if result.get("commit"):
            console.print(f"  commit: [dim]{result['commit'][:12]}[/]")
```

然后在 `main.py` 注册命令组。在 `main.py:336` 附近添加：

```python
from .project import project  # noqa: E402

cli.add_command(project)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_knowledge.py::TestProjectCLI -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/cli/project.py src/story_lifecycle/cli/main.py tests/test_knowledge.py
git commit -m "feat(knowledge): add story project init-knowledge and sync-knowledge CLI commands"
```

---

### Task 8: Stale 检测

**Files:**
- Create: `src/story_lifecycle/knowledge/stale.py`
- Modify: `tests/test_knowledge.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_knowledge.py`：

```python
from story_lifecycle.knowledge.stale import check_stale


class TestStale:
    def _write_manifest(self, tmp_path, commit="abc123", ts="2026-01-01T00:00:00"):
        from story_lifecycle.knowledge import paths as kp
        manifest = {
            "version": 1,
            "source": {"commit": commit, "timestamp": ts, "dirty": False},
            "status": "ready",
        }
        kp.manifest_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        kp.manifest_path(tmp_path).write_text(
            yaml.dump(manifest), encoding="utf-8"
        )

    def test_fresh_when_commit_matches(self, tmp_path, monkeypatch):
        """manifest commit 与当前 HEAD 相同 → 不是 stale。"""
        self._write_manifest(tmp_path, commit="abc123def456")
        monkeypatch.setattr(
            "story_lifecycle.knowledge.stale._get_git_commit",
            lambda w: "abc123def456",
        )
        result = check_stale(tmp_path)
        assert not result["stale"]

    def test_stale_when_commit_differs(self, tmp_path, monkeypatch):
        """manifest commit 与当前 HEAD 不同 → stale。"""
        self._write_manifest(tmp_path, commit="old_commit")
        monkeypatch.setattr(
            "story_lifecycle.knowledge.stale._get_git_commit",
            lambda w: "new_commit",
        )
        result = check_stale(tmp_path)
        assert result["stale"]
        assert "commit" in result["reason"]

    def test_stale_when_no_manifest(self, tmp_path):
        """manifest 不存在 → stale。"""
        result = check_stale(tmp_path)
        assert result["stale"]

    def test_stale_when_manifest_status_is_stale(self, tmp_path):
        """manifest status 已经是 stale → stale。"""
        from story_lifecycle.knowledge import paths as kp
        manifest = {
            "version": 1,
            "source": {"commit": "abc", "timestamp": "2026-01-01"},
            "status": "stale",
        }
        kp.manifest_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        kp.manifest_path(tmp_path).write_text(
            yaml.dump(manifest), encoding="utf-8"
        )
        result = check_stale(tmp_path)
        assert result["stale"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_knowledge.py::TestStale -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 写实现**

```python
# src/story_lifecycle/knowledge/stale.py
"""检测知识包是否过期（stale）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import manifest_path


def _get_git_commit(workspace: str | Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(workspace), timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def check_stale(workspace: str | Path) -> dict:
    """返回 {"stale": bool, "reason": str, "commit": str|None}。"""
    import yaml

    mp = manifest_path(workspace)

    if not mp.exists():
        return {"stale": True, "reason": "manifest.yaml 不存在", "commit": None}

    try:
        data = yaml.safe_load(mp.read_text(encoding="utf-8"))
    except Exception as e:
        return {"stale": True, "reason": f"manifest 解析失败: {e}", "commit": None}

    if not isinstance(data, dict):
        return {"stale": True, "reason": "manifest 格式错误", "commit": None}

    # 状态已标记为 stale
    if data.get("status") == "stale":
        return {
            "stale": True,
            "reason": "manifest 状态已标记为 stale",
            "commit": data.get("source", {}).get("commit"),
        }

    # 对比 commit
    source = data.get("source", {})
    saved_commit = source.get("commit", "")
    current_commit = _get_git_commit(workspace)

    if current_commit and saved_commit and current_commit != saved_commit:
        return {
            "stale": True,
            "reason": f"commit 变化: {saved_commit[:12]} → {current_commit[:12]}",
            "commit": current_commit,
        }

    return {"stale": False, "reason": "", "commit": current_commit or saved_commit}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_knowledge.py::TestStale -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/stale.py tests/test_knowledge.py
git commit -m "feat(knowledge): add stale detection for knowledge pack freshness"
```

---

### Task 9: 最小结构化搜索工具

**Files:**
- Create: `src/story_lifecycle/knowledge/search.py`
- Modify: `tests/test_knowledge.py`

P1 要求提供一个最小搜索工具，避免 LLM 直接拼 shell。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_knowledge.py`：

```python
from story_lifecycle.knowledge.search import search_knowledge


class TestSearch:
    def _setup_index(self, tmp_path, content):
        """辅助：创建一个索引文件。"""
        from story_lifecycle.knowledge import paths as kp
        from story_lifecycle.knowledge.scaffold import scaffold_knowledge_dir
        scaffold_knowledge_dir(tmp_path)
        idx = kp.indexes_dir(tmp_path) / "api-index.md"
        idx.write_text(content, encoding="utf-8")

    def test_search_finds_keyword(self, tmp_path):
        self._setup_index(tmp_path, "# API Index\n\n## /api/withdraw\n提现接口\n")
        results = search_knowledge(str(tmp_path), keyword="withdraw")
        assert len(results) > 0
        assert any("withdraw" in r["line"].lower() for r in results)

    def test_search_by_type_filter(self, tmp_path):
        self._setup_index(tmp_path, "# API Index\n\n## /api/withdraw\n")
        results = search_knowledge(
            str(tmp_path), keyword="withdraw", target_type="api"
        )
        # target_type 限制搜索路径包含 api
        assert all("api" in r["file"] for r in results)

    def test_search_no_results(self, tmp_path):
        self._setup_index(tmp_path, "# API Index\nnothing here\n")
        results = search_knowledge(str(tmp_path), keyword="nonexistent_xyz")
        assert results == []

    def test_search_limit(self, tmp_path):
        content = "# API Index\n" + "\n".join(
            f"## /api/withdraw/{i}\nwithdraw endpoint {i}\n"
            for i in range(50)
        )
        self._setup_index(tmp_path, content)
        results = search_knowledge(str(tmp_path), keyword="withdraw", limit=5)
        assert len(results) <= 5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_knowledge.py::TestSearch -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 写实现**

```python
# src/story_lifecycle/knowledge/search.py
"""知识包结构化搜索 — 最小版本。

避免 LLM 直接拼接 shell 命令，提供结构化参数接口。
"""

from __future__ import annotations

import re
from pathlib import Path

from .paths import knowledge_dir

# 类型 → 搜索路径映射
_TYPE_PATHS: dict[str, list[str]] = {
    "api": ["indexes/api-index.md"],
    "table": ["indexes/table-index.md"],
    "field": ["indexes/field-index.md"],
    "mq": ["indexes/mq-index.md"],
    "service": ["indexes/service-index.md"],
    "scenario": ["scenarios/"],
    "state_machine": ["indexes/state-machine-index.md"],
    "enum": ["indexes/enum-index.md"],
    "bug": ["indexes/bug-risk-index.md"],
    "test_case": ["indexes/test-case-index.md"],
    "text": [],  # 搜索全部
}


def search_knowledge(
    workspace: str | Path,
    keyword: str,
    target_type: str = "text",
    limit: int = 20,
) -> list[dict]:
    """在知识包中搜索关键词。

    参数:
        workspace: 项目工作区路径
        keyword: 搜索关键词（自动转义正则特殊字符）
        target_type: 限制搜索的索引类型 (api|table|field|mq|service|scenario|text)
        limit: 最大返回条目数

    返回:
        [{"file": str, "line": str, "line_no": int}]
    """
    root = knowledge_dir(workspace)
    if not root.exists():
        return []

    # 确定搜索目标文件
    search_paths = _resolve_paths(root, target_type)

    # 安全转义关键词
    pattern = re.escape(keyword)

    results: list[dict] = []
    for sp in search_paths:
        if sp.is_dir():
            results.extend(_search_dir(sp, pattern, limit - len(results)))
        elif sp.exists():
            results.extend(_search_file(sp, pattern, limit - len(results)))

        if len(results) >= limit:
            break

    return results[:limit]


def _resolve_paths(root: Path, target_type: str) -> list[Path]:
    """根据类型确定搜索路径。"""
    if target_type == "text":
        return [root]

    rel_paths = _TYPE_PATHS.get(target_type, [])
    paths = []
    for rp in rel_paths:
        p = root / rp
        if p.exists():
            paths.append(p)

    # 如果指定了类型但对应文件不存在，退回全搜索
    if not paths:
        paths = [root]

    return paths


def _search_file(path: Path, pattern: str, limit: int) -> list[dict]:
    results = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return results

    rel = str(path)
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line, re.IGNORECASE):
            results.append({"file": rel, "line": line.strip(), "line_no": i})
            if len(results) >= limit:
                break
    return results


def _search_dir(dirpath: Path, pattern: str, limit: int) -> list[dict]:
    results = []
    for f in dirpath.rglob("*.md"):
        results.extend(_search_file(f, pattern, limit - len(results)))
        if len(results) >= limit:
            break
    return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_knowledge.py::TestSearch -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/knowledge/search.py tests/test_knowledge.py
git commit -m "feat(knowledge): add minimal structured search tool for knowledge files"
```

---

### Task 10: 集成测试 + 创建 story 时的知识包提示

**Files:**
- Modify: `tests/test_knowledge.py`
- 修改 `src/story_lifecycle/cli/main.py` 的 `create` 命令

设计文档要求：首次创建 story 时，如果缺少 `manifest.yaml`，只提示不自动生成。

- [ ] **Step 1: 写测试**

追加到 `tests/test_knowledge.py`：

```python
class TestCreateStoryKnowledgeHint:
    """创建 story 时，如果缺少知识包，应给出提示。"""

    def test_create_without_knowledge_shows_hint(self, tmp_path, monkeypatch):
        """没有 .story/knowledge/manifest.yaml 时，create 命令应提示。"""
        monkeypatch.setattr(
            "story_lifecycle.cli.main.init_db", lambda: None
        )
        monkeypatch.setattr(
            "story_lifecycle.cli.main.is_configured", lambda: True
        )
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.service.create_and_start_story",
            lambda **kw: kw["story_key"],
        )
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.graph.start_story_async",
            lambda key: None,
        )
        # load_config_to_env 不报错
        monkeypatch.setattr(
            "story_lifecycle.cli.main.load_config_to_env", lambda: None
        )

        result = CliRunner().invoke(
            cli, ["create", "TEST-001", "-t", "test", "-w", str(tmp_path), "--no-start"]
        )
        assert "init-knowledge" in result.output or "知识包" in result.output
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_knowledge.py::TestCreateStoryKnowledgeHint -v`
Expected: FAIL — 输出中不包含提示

- [ ] **Step 3: 在 create 命令中添加提示**

在 `src/story_lifecycle/cli/main.py` 的 `create` 函数中，在 `console.print(f"\n[green]Story created:[/]...")` 之前添加：

```python
    # 检查知识包是否存在
    from ..knowledge.paths import manifest_path as _km
    if not _km(ws).exists():
        console.print(
            "[yellow]当前项目尚未初始化项目知识包。建议先运行：[/]\n"
            "  [bold]story project init-knowledge[/]\n"
            "[dim]继续创建 story 也可以，但 AI 将缺少项目级业务/代码上下文。[/]\n"
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_knowledge.py::TestCreateStoryKnowledgeHint -v`
Expected: PASS

- [ ] **Step 5: 运行全部知识模块测试**

Run: `pytest tests/test_knowledge.py -v`
Expected: 全部通过（约 30+ tests）

- [ ] **Step 6: 运行完整测试套件确认无回归**

Run: `pytest -x`
Expected: 全部通过

- [ ] **Step 7: Commit**

```bash
git add src/story_lifecycle/cli/main.py tests/test_knowledge.py
git commit -m "feat(knowledge): show knowledge pack hint on story create when missing"
```

---

## Self-Review

### 1. Spec 覆盖检查

| 设计文档要求 | 对应 Task |
|-------------|-----------|
| `.story/knowledge/` 目录标准 | Task 1 (paths) + Task 2 (scaffold) |
| manifest/product/search-catalog 模板 | Task 3 (templates) |
| graph schema (节点/关系类型) | Task 3 (graph-schema.json) |
| scenario 模板 | Task 3 (scenario.md) |
| index 模板 | Task 3 (index.md) |
| bootstrap prompt 模板 | Task 4 |
| context builder prompt 模板 | **P2 范围，不阻塞 P1** |
| `story project init-knowledge` 命令 | Task 7 |
| `story project sync-knowledge` 命令 | Task 7 (CLI) + Task 8 (stale 检测) |
| CLI headless 执行 bootstrap | Task 5 (bootstrap runner) |
| 校验关键产物和 done JSON | Task 6 (validator) |
| 结构化 Search Tool 最小版本 | Task 9 |
| 创建 story 时知识包缺失提示 | Task 10 |
| .gitignore 规则 | Task 2 |
| Knowledge 状态标记 (extracted/proposed/verified) | Task 4 (prompt 模板中定义规则) |
| source_refs 规则 | Task 4 (prompt 模板中定义规则) |

**未覆盖（属于 P2/P3）：**
- Context Builder 工作流 (P2)
- describe/search/expand/compose 协议的 Python API 化 (P3)
- token budget 裁剪 (P3)
- Manual declarations 的读取和验证 (P2)
- Playbook 生成 (P2)
- 事件文件 local-skill-events.jsonl (P4)

### 2. Placeholder 扫描

无 TBD、TODO、"implement later" 等占位符。所有代码步骤包含完整实现。

### 3. 类型一致性检查

- `render_bootstrap_prompt()` 在 Task 5 定义，Task 7 CLI 中调用 — 签名一致
- `validate_knowledge_pack()` 在 Task 6 定义，Task 7 CLI 中调用 — 签名一致
- `check_stale()` 在 Task 8 定义，Task 7 sync-knowledge 命令中调用 — 签名一致
- `search_knowledge()` 在 Task 9 定义 — 独立使用，无跨 Task 调用
- 所有 `paths.py` 函数在 Task 1 定义，后续 Task 均引用 — 一致
- `scaffold_knowledge_dir()` 在 Task 2 定义，Task 6/7/9 中使用 — 一致
