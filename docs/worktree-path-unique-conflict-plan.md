# 修复方案:worktree_path UNIQUE 冲突(NULL 版)

- 创建日期: 2026-07-02
- 状态: 已实现 (2026-07-02) — 全量 631 passed / 2 skipped,无回归
- 取代: `banshee-winter-soldier-vision.md`(原评审计划)中的"占位符 + 迁移 unprepared"机制
- 上下文: [worktree-path-unique-conflict.md](./worktree-path-unique-conflict.md)(事故诊断 / 根因 / 案例 / 临时绕过)

> 路径说明:本计划所有文件路径已于 2026-07-02 用 Glob/grep 对实盘核实。codegraph 索引显示的 `orchestrator/api.py` / `orchestrator/worktree/` 是过期别名,实盘为 `orchestrator/service/api.py` / `orchestrator/workspace/worktree/`。行号来自 codegraph 的"re-read from disk"(旧路径已不存在,故内容/行号即当前文件)。

## 与原计划的关键差异(为什么改)

读真实代码后,原计划的 centerpiece 是多余的:

| 原计划 | 本方案 | 原因 |
|---|---|---|
| `_pending_{story_key}_{project_id}` 占位字符串 | **存 NULL** | SQLite 的 `UNIQUE` 对 NULL 豁免(多 NULL 互不冲突),列已是 `TEXT UNIQUE`,NULL 直接生效,**零 schema 迁移** |
| 步骤 1.2:INSERT 前"检查占用 → 迁移 unprepared → INSERT" | **删掉检查阶段,改为 try-INSERT / catch IntegrityError** | WAL + deferred 隔离下检查-再写是 TOCTOU;让 UNIQUE 约束本身当串行点才正确 |
| 步骤 3.1:REJECT 时一律自动建外部 worktree | **仅 `PATH_CONFLICT` 自动建,其余真 reject** | `decide_prepare` 有 6 种 REJECT,其中 `NO_BRANCH_NAME`/`STALE`/`BRANCH_CHECKED_OUT_ELSEWHERE` 自动建是错的 |
| 步骤 4:用 `workspace_type='main_checkout'` 当门禁 | **直接删掉 repo_path fallback,不需要门禁** | `workspace_type` 全仓无人写入(默认 `""`),门禁是空的;主 checkout 复用走 `worktree_path` 真值即可 |

附带修掉一个潜伏 bug:`prepare_worktrees` 见 `worktree_path` 非空就拿它当真路径建 worktree(`handler.py:84`),占位符让它拿 `_pending_...` 假串去 `git worktree add`。换 NULL 后正确落入 `worktree_root/story_key/project_name` 推导。

## 方案概述

**保持 `worktree_path TEXT UNIQUE` 不变。** 把"还没有 worktree"从塞假字符串改成存 NULL。由此:

1. 多个 unprepared 绑定都是 NULL,互不冲突——UNIQUE 只在"真实路径 vs 真实路径"时触发;
2. `bind_story_project` / `update_story_project` 用 `INSERT ... catch IntegrityError` 处理冲突:占用者陈旧(`unprepared`/`missing`)则自动迁移其路径到 NULL 并重试,占用者活跃(`available` 等)则抛 `WorktreePathConflict` → API 返回 409;
3. `PUT /context/branch` 区分"未提供 worktree_path"(no-op)与"显式空串"(清空到 NULL);
4. `prepare_worktrees` 在 `PATH_CONFLICT` 时自动改走外部 worktree;`worktree_root` 未配置时降级到 `<repo>/.worktrees/<story_key>`;
5. `auto_discovery` 删除 repo_path fallback,worktree 未就绪时返回 MISSING 错误,不扫错分支。

## worktree 生成流程(背景,澄清"NULL 后 worktree 怎么来")

绑定拿到真实路径只有两条路:
- **路线 A(默认)**:`prepare_worktrees` 现场拼 `worktree_root/story_key/project_name` → `git worktree add` → 把真实路径写回 DB(NULL → 真路径),state→`available`。路径含 story_key,天然唯一,**永不撞 UNIQUE**。
- **路线 B(显式)**:`PUT /context/branch` 直接传主 checkout 路径(如 `D:/hc-all/hc-user`)。唯一可能撞 UNIQUE 的情况 → 409。

NULL 不影响生成:路线 A 的路径推导不读绑定里存的值,占位符只是在添乱。

---

## 具体步骤

### 1. DB 层 — `packages/story-lifecycle/src/story_lifecycle/db/models.py`

#### 1.1 新增异常(放在 `bind_story_project` 之前)

```python
class WorktreePathConflict(Exception):
    """worktree_path 已被一个活跃绑定占用,无法登记。"""

    def __init__(self, worktree_path: str, occupant: dict):
        self.worktree_path = worktree_path
        self.occupant = occupant
        super().__init__(
            f"worktree_path {worktree_path} 已被 story {occupant.get('story_key')} "
            f"占用 (state={occupant.get('worktree_state')})"
        )
```

#### 1.2 新增私有冲突解决器

陈旧状态可自动迁移;`available` 等活跃状态抛冲突。`WorktreeState` 取值见 `orchestrator/workspace/worktree/resolver.py:25-33`(`unprepared/available/missing/stale/conflict/unknown`)。

```python
# 可被自动迁移(释放路径)的占用者状态:肯定没有活跃 worktree
_DISPLACEABLE_STATES = {"unprepared", "missing"}


def _find_worktree_occupant(worktree_path: str) -> dict | None:
    """新开只读连接查 worktree_path 的当前占用者(bind 事务已因异常退出)。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT story_key, project_id, worktree_state, branch "
            "FROM story_project WHERE worktree_path = ?",
            (worktree_path,),
        ).fetchone()
    return dict(row) if row else None


def _resolve_worktree_conflict(worktree_path: str) -> None:
    """INSERT/UPDATE 撞 worktree_path UNIQUE 时调用。
    占用者陈旧 → 把它置 NULL 并返回(调用方重试);活跃 → 抛 WorktreePathConflict。"""
    occupant = _find_worktree_occupant(worktree_path)
    if not occupant:
        return  # 不是 worktree_path 冲突(可能是 (story_key,project_id) 重复),交还调用方
    if occupant.get("worktree_state") in _DISPLACEABLE_STATES:
        with _db() as conn:
            conn.execute(
                "UPDATE story_project SET worktree_path = NULL, "
                "updated_at = ? WHERE story_key = ? AND project_id = ?",
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    occupant["story_key"],
                    occupant["project_id"],
                ),
            )
        return
    raise WorktreePathConflict(worktree_path, occupant)
```

#### 1.3 改 `bind_story_project`(`models.py:1730-1774`)

```python
# 原 line 1745-1747:
# if not worktree_path:
#     worktree_path = f"_pending_{story_key}_{project_id}"
# 改为:
if not worktree_path:
    worktree_path = None  # TEXT UNIQUE 对 NULL 豁免;未建 worktree 时存 NULL,不造假路径
```

并把 INSERT 包进 try/except:

```python
try:
    with _db() as conn:
        conn.execute(
            """INSERT INTO story_project (...) VALUES (...)""",
            (...),
        )
        row = conn.execute(
            "SELECT * FROM story_project WHERE story_key = ? AND project_id = ?",
            (story_key, project_id),
        ).fetchone()
    return dict(row) if row else {}
except sqlite3.IntegrityError:
    if worktree_path:  # 只处理 worktree_path 维度的冲突
        _resolve_worktree_conflict(worktree_path)
        # 陈旧占用者已置 NULL,重试一次
        with _db() as conn:
            conn.execute("""INSERT INTO story_project (...) VALUES (...)""", (...))
            row = conn.execute(
                "SELECT * FROM story_project WHERE story_key = ? AND project_id = ?",
                (story_key, project_id),
            ).fetchone()
        return dict(row) if row else {}
    raise  # 其他 IntegrityError 原样抛
```

> 注:`_resolve_worktree_conflict` 对活跃占用者抛 `WorktreePathConflict`,不会走到重试;只有陈旧占用者(已置 NULL)才重试,此时重试 INSERT 必成功。

#### 1.4 改 `update_story_project`(`models.py:1797-1822`)

UPDATE 设 `worktree_path` 同样可能撞 UNIQUE。当 kwargs 含 `worktree_path` 且非 None 时,包同样逻辑:

```python
def update_story_project(story_key, project_id, **kwargs):
    ...  # 现有 valid 校验、updated_at 不变
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [story_key, project_id]
    try:
        with _db() as conn:
            conn.execute(
                f"UPDATE story_project SET {sets} WHERE story_key = ? AND project_id = ?",
                values,
            )
    except sqlite3.IntegrityError:
        wp = kwargs.get("worktree_path")
        if wp:  # worktree_path 维度冲突
            _resolve_worktree_conflict(wp)
            with _db() as conn:
                conn.execute(
                    f"UPDATE story_project SET {sets} WHERE story_key = ? AND project_id = ?",
                    values,
                )
            return
        raise
```

### 2. API 层 — `packages/story-lifecycle/src/story_lifecycle/orchestrator/service/api.py`

#### 2.1 `api_set_branch`(`service/api.py:1869-1895`)捕获冲突 → 409,并区分空串语义

```python
@app.put("/api/story/{story_key}/context/branch")
def api_set_branch(story_key: str, req: SetBranchRequest):
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    try:
        existing = db.get_story_project(story_key, req.project_id)
        fields: dict = {"branch": req.branch}
        # 区分 None(未提供=不动)与 ""(显式清空到 NULL)
        if req.worktree_path is None:
            pass
        elif req.worktree_path == "":
            fields["worktree_path"] = None  # 清空,释放主 checkout
        else:
            fields["worktree_path"] = req.worktree_path
        if req.base_branch is not None:
            fields["base_branch"] = req.base_branch
        if req.worktree_state:
            fields["worktree_state"] = req.worktree_state
        if existing:
            db.update_story_project(story_key, req.project_id, **fields)
        else:
            fields.setdefault("base_branch", "main")
            db.bind_story_project(story_key, req.project_id, **fields)
        db.bump_context_revision(story_key)
        return db.get_story_project(story_key, req.project_id)
    except db.WorktreePathConflict as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"worktree_path {e.worktree_path} 已被 story "
                    f"{e.occupant.get('story_key')} 占用 "
                    f"(state={e.occupant.get('worktree_state')})。"
                    f"用 worktree_path='' 清空旧绑定,或 POST /worktrees/prepare 建独立 worktree。"
                ),
                "occupant_story_key": e.occupant.get("story_key"),
                "occupant_state": e.occupant.get("worktree_state"),
                "worktree_path": e.worktree_path,
            },
        )
```

> **幂等性**:`worktree_path=""` 清空时,若当前已是 NULL,再 `UPDATE ... SET worktree_path=NULL` 无副作用,幂等。✓

### 3. Worktree 准备 — `packages/story-lifecycle/src/story_lifecycle/orchestrator/workspace/worktree/handler.py`

#### 3.1 抽出路径推导 + `.worktrees/` 兜底

参考同仓 `github-ops/scripts/converge/git_ops.py:100-130` 的 `create_worktree` / `_ensure_ignored` 范式(仓库本地 `.worktrees/<id>` + 写 `.git/info/exclude`,不污染目标仓)。

```python
def _ensure_local_exclude(repo: Path, pattern: str) -> None:
    """把 pattern 加到目标仓 .git/info/exclude(纯本地,不改 .gitignore)。"""
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as f:
            f.write(f"{pattern}\n")


def _derive_worktree_path(sp: dict, project: dict, story_key: str, worktree_root: str) -> str:
    """决定本次 prepare 用哪个路径。不读占位符,只认真实路径 / worktree_root / 仓库本地兜底。"""
    if sp.get("worktree_path"):           # 路线 B:显式指定(主 checkout 复用)
        return sp["worktree_path"]
    if worktree_root:                      # 路线 A:外部隔离 worktree
        return str(Path(worktree_root) / story_key / project["name"])
    # 兜底:仓库本地 .worktrees/<story_key>
    repo = Path(project["repo_path"])
    _ensure_local_exclude(repo, ".worktrees/")
    return str(repo / ".worktrees" / story_key)
```

#### 3.2 CREATE 分支用 `_derive_worktree_path`(`handler.py:83-98` 替换)

把 `if sp.get("worktree_path"): ... elif worktree_root: ... else: reject(...)` 三段替换为:

```python
wt_path = _derive_worktree_path(sp, project, story_key, worktree_root)
```

(原来的 "no worktree_path and no worktree_root configured" reject 不再出现——有 `.worktrees/` 兜底。)

#### 3.3 REJECT 分支:`PATH_CONFLICT` 自动改走外部 worktree(`handler.py:141-149` 替换)

```python
else:  # PrepareAction.REJECT
    if decision.reject_reason == RejectReason.PATH_CONFLICT and sp.get("worktree_path"):
        # 显式指定的主 checkout 被占 → 改走外部独立 worktree
        sp_ext = {**sp, "worktree_path": None}  # 强制走推导,不复用被占路径
        wt_path = _derive_worktree_path(sp_ext, project, story_key, worktree_root)
        try:
            _ensure_branch(repo_path, sp.get("branch", ""), sp.get("base_branch", "main"), sp.get("base_commit", ""))
            _create_worktree(repo_path, wt_path, sp["branch"])
            db.update_story_project(story_key, sp["project_id"],
                                    worktree_path=wt_path, worktree_state=WorktreeState.AVAILABLE,
                                    workspace_type="worktree")
            results.append({"story_project": sp, "action": "create_fallback",
                            "worktree_path": wt_path, "error": None})
        except Exception as e:
            results.append({"story_project": sp, "action": "reject",
                            "worktree_path": None, "error": f"fallback create failed: {e}"})
    else:
        # NO_BRANCH_NAME / STALE / BRANCH_CHECKED_OUT_ELSEWHERE / PROJECT_NOT_FOUND / BRANCH_EXISTS → 真 reject
        results.append({"story_project": sp, "action": "reject",
                        "worktree_path": None, "error": decision.reason})
```

> 仅 `PATH_CONFLICT` 且原绑定显式指定了路径(路线 B)时才降级;路线 A 绑定本就是 NULL,不会进这里。

### 4. Auto-discovery — `packages/story-lifecycle/src/story_lifecycle/orchestrator/context/auto_discovery.py:62-71`

删除 repo_path fallback。主 checkout 复用(路线 B)的 worktree_path 是真实存在路径,仍走第一分支正常扫;只有"未准备 worktree"或"路径已不存在"才进错误分支。

```python
# 原:
# if worktree_path and Path(worktree_path).exists():
#     scan_root = worktree_path
# elif repo_path and Path(repo_path).exists():
#     scan_root = repo_path
#     fallback = True
# else:
#     return ScanResult(errors=[...])
# 改为:
if worktree_path and Path(worktree_path).exists():
    scan_root = worktree_path
else:
    # 不再 fallback 到 repo_path:宁可不扫,也不扫错分支污染上下文。
    # worktree_path 为 NULL(未准备)或路径不存在(已删除/未创建)都走这里。
    return ScanResult(
        project_id=project_id,
        errors=[
            f"worktree 未就绪 (worktree_path={worktree_path!r});"
            f"请先 POST /worktrees/prepare"
        ],
    )
```

`ScanResult.fallback_mode` 字段保留(默认 False,不再被置 True),向后兼容。

### 5. 数据迁移(一次性)

把现有 `_pending_...` 占位行(含诊断文档里的手动绕过产物)置 NULL:

```sql
UPDATE story_project
SET worktree_path = NULL,
    updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now')
WHERE worktree_path LIKE '_pending_%';
```

> 陈旧的真实路径行(如诊断案例 `1065570`:`worktree_path=D:/hc-all/hc-user`、`state=unprepared`)**不在迁移范围**——它们在运行时由步骤 1.2 的冲突解决器自动迁移(占用者 unprepared → 置 NULL → 新绑定接管),或由步骤 2.1 的 `worktree_path=""` 显式清空。

### 6. 前置确认(已执行,2026-07-02)

- [x] **`""` 调用方 = 无**:`PUT /context/branch` 仅被测试与 agent(curl)调用;CLI(`cli/`)与前端(`frontend/`)均不发送 worktree_path(前端仅在 `ContextTab.tsx:8` 读取展示)。测试发送 `{project_id, branch}` 不含 worktree_path。**翻 `""` 语义无破坏性,可安全实施。**
- [x] **gov = 不在本仓**:`D:/github/story-lifecycle-gov` 目录不存在,本仓无 `.gitmodules`,无嵌套 gov 目录。codegraph 里的 gov 索引指向本机之外的代码库。**本计划改动自包含,无需 double-apply。**(若别处有 gov 部署,需在其仓单独修。)

## 测试

`packages/story-lifecycle/tests/`:

**DB 层**(`test_db_models.py` 或新建):
- `test_multiple_unprepared_bindings_coexist` — 多个 story_project 不传 worktree_path,全部 NULL,无碰撞。
- `test_bind_displaces_stale_unprepared_occupant` — 占用者 `state=unprepared` 时,新 bind 同路径成功,占用者路径变 NULL。
- `test_bind_raises_409_on_active_occupant` — 占用者 `state=available` 时,新 bind 同路径抛 `WorktreePathConflict`。
- `test_update_worktree_path_conflict_displaces_or_409` — update 路径同上两种情形。
- `test_no_pending_placeholder_in_db` — 绑定后查表,断言无 `_pending_%` 字符串(防回退)。

**API 层**(`test_context_write.py` 或新建 `test_api_context.py`):
- `test_set_branch_returns_409_with_occupant_when_active` — 409 body 含 `occupant_story_key` / `worktree_path`。
- `test_set_branch_empty_string_clears_to_null` — 传 `worktree_path=""`,绑定 worktree_path 变 NULL;再调一次(已 NULL)幂等。
- `test_set_branch_omitted_worktree_path_noop` — 不传 worktree_path,原值不变。

**Worktree handler**(`test_worktree.py`):
- `test_prepare_derives_external_path_from_worktree_root` — worktree_path 为 NULL,prepare 后路径 = `worktree_root/story_key/project_name`。
- `test_prepare_falls_back_to_local_dotworktrees` — 未配置 worktree_root,路径 = `<repo>/.worktrees/<story_key>`,且 `.git/info/exclude` 含 `.worktrees/`。
- `test_prepare_path_conflict_creates_external_fallback` — 显式主 checkout 被 `PATH_CONFLICT` 拒 → 自动建外部 worktree。
- `test_prepare_no_branch_name_still_rejects` — `NO_BRANCH_NAME` 不触发降级,真 reject。

**Auto-discovery**(`test_auto_discovery.py`):
- `test_scan_returns_missing_when_worktree_null` — worktree_path 为 NULL → 返回 errors,不扫 repo_path。
- `test_scan_returns_missing_when_path_not_on_disk` — worktree_path 非空但不存在 → 同上。
- `test_scan_main_checkout_via_worktree_path` — 路线 B 设主 checkout 路径且存在 → 正常扫(走第一分支,非 fallback)。

## 验证清单

- [ ] 两 story 均不指定 worktree_path → 各自 NULL,均 200,互不阻塞。
- [ ] story A 占主 checkout(`available`);story B 登记同路径 → 409,带 `occupant_story_key=A`。
- [ ] story A 占主 checkout 但 `state=unprepared`(陈旧);story B 登记同路径 → 200,A 自动置 NULL。
- [ ] `PUT /context/branch` 传 `worktree_path=""` → 当前绑定清为 NULL;重复调用幂等。
- [ ] `POST /worktrees/prepare` 主 checkout 被 `PATH_CONFLICT` → 自动建 `D:/worktrees/{story_key}/{service}`(或 `<repo>/.worktrees/{story_key}`)。
- [ ] auto-discovery 在 worktree_path 为 NULL/不存在时返回 MISSING,不扫 repo_path。
- [ ] 全量测试通过:`pytest packages/story-lifecycle/tests/ -q`。

## 不做的范围

- 不改 `worktree_path TEXT UNIQUE` 约束(改用 NULL 绕过,而非放宽约束——放宽已验证会污染)。
- 不引入"主 checkout 多 story 共享"语义(物理 worktree 有状态,共享必污染)。
- 不加 `workspace_type` 门禁(全仓无写入点;主 checkout 复用由 worktree_path 真值天然区分)。`workspace_type` 仅在 prepare 时设 `'worktree'` 作信息记录,不作判断依据。
- 不改 codegraph 工具本身。
- 物理 worktree GC:`cleanup_worktrees` + `decide_cleanup`(`handler.py:154+`)已存在,门禁 `delivery_state ∈ {merged, abandoned}`。本计划不含"story 归档 → 调 cleanup + `git worktree prune`"的接线,建议作为后续独立任务。

## 遗留决策点

1. **可迁移状态范围**:当前只自动迁移 `unprepared`/`missing`。`stale`/`conflict`/`unknown` 命中时返回 409 让人工处理。是否扩展到 `stale` 待定(可能有 live 但分支不匹配的 worktree,贸然释放风险高)。
