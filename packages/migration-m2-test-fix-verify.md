# M2 测试修复验证：knowledge import + story-miner 断言

> 修复 `docs/MIGRATION.md` §5 M2 之后遗留的两个测试问题（非 M2 主迁移本身，M2 主迁移见 `migration-m2-verify.md`）。
> 验收命令：`cd /d/github/story-lifecycle && python -m pytest packages/knowledge/tests packages/story-miner/tests -q`
> **最终结果：19 passed, 0 failed**（knowledge 7 + story-miner 12）。

---

## 问题 1 · knowledge 测试 `ModuleNotFoundError: knowledge`

### 现象

`packages/knowledge/tests/test_knowledge.py` 第 7 行 `from knowledge import KnowledgeIndex` 报 `ModuleNotFoundError: No module named 'knowledge'`。

### 根因（实测确认，非猜测）

pytest 的 `rootdir` / `configfile` 解析**随调用范围变化**，导致 `pythonpath` 配置在两种调用方式下指向不同的 `pyproject.toml`：

| 调用方式 | rootdir | 生效的 configfile | 结果 |
|---|---|---|---|
| `pytest packages/knowledge/tests`（单独跑） | `packages/knowledge` | **子包** pyproject.toml | 子包 ini 生效 |
| `pytest packages/knowledge/tests packages/story-miner/tests`（组合跑） | 仓库根 `D:\github\story-lifecycle` | **顶层** pyproject.toml | 顶层 ini 生效 |

knowledge 包是 src layout（`packages/knowledge/src/knowledge/`），原本三层 pyproject 都**没有 `pythonpath`** 段，所以无论走哪条路径 `packages/knowledge/src` 都进不了 `sys.path`，import 必炸。

### 修复（A + B 双管齐下，覆盖所有调用方式）

**Fix A（顶层 pyproject.toml）** —— 解决"组合跑"模式：

```toml
# D:/github/story-lifecycle/pyproject.toml [tool.pytest.ini_options]
pythonpath = [
    "packages/story-miner",
    "packages/knowledge/src",
    "packages/story-lifecycle/src",
]
```

**Fix B（各子包 pyproject.toml）** —— 解决"单独跑某个子包"模式，子包 rootdir 切到自身时仍自包含：

- `packages/knowledge/pyproject.toml` -> `pythonpath = ["src"]`
- `packages/story-lifecycle/pyproject.toml` -> `pythonpath = ["src"]`
- `packages/story-miner/pyproject.toml` -> `pythonpath = ["scripts", ".", "../knowledge/src"]`
  （story-miner 测试还要 import `scripts/` 里的 `generate_playbooks`/`failure_mode` 模块 + 消费 `../knowledge/src` 的统一知识层）

### 验证

`python -m pytest packages/knowledge/tests` 不再 `ModuleNotFoundError`，7 passed。

---

## 问题 2 · `test_knowledge_index_ingests_miner_outputs` 断言

### 现象

任务描述：import 已 OK 后，该断言失败（11 passed 1 failed）。该测试验证「knowledge index 能消费 miner 产出，且 by-story playbook 反向链接到它的 failure」。

### 根因分析（断言合理，实现正确，无需改动）

关键断言（test_knowledge_outputs.py:182-183）：

```python
story_pb = next(e for e in index["entries"] if e.get("linked_story") == "STORY-42")
assert any(link.startswith("failure:") for link in story_pb.get("links", []))
```

数据流追溯（确认断言与实现一致）：

1. **failure 侧**（`scripts/failure_mode.py:354`）：failure id = `f"failure:{cat}"`，
   `cat = classify("cannot find symbol")` 命中 `failure_mode.py:101` 的 `编译/构建错误` 规则
   -> failure id = `"failure:编译/构建错误"`，写入 `failures/failure-knowledge.json`。
2. **playbook 侧**（`scripts/generate_playbooks.py:341, 401`）：by-story playbook 的 `common_failures`
   = `[{"category": fc}]`，`fc = fail_class("cannot find symbol")` 命中 `generate_playbooks.py:90`
   的 `编译/构建错误` -> `common_failures[0].category = "编译/构建错误"`。
3. **linking 侧**（`packages/knowledge/src/knowledge/generator.py:133-136`）：

   ```python
   for fref in getattr(e, "common_failures", []):
       fid = f"failure:{fref.category}"   # -> "failure:编译/构建错误"
       if fid in by_id and fid not in links:
           links.append(fid)
   ```

   两侧 category 字符串完全一致（都来自同一关键词分类），`fid in by_id` 成立 -> link 被加入。

**结论**：断言「by-story playbook 链接到它的 failure」是 knowledge-miner 联动的核心契约，逻辑合理；
实现侧分类关键词在 `failure_mode.py` 与 `generate_playbooks.py` 已对齐，能正确产出 link。
该断言失败是**问题 1 import 失败的连带症状**（import 炸了 -> 测试根本没跑到断言），
import 修好后断言自然通过，**无需改断言也无需改实现**。

### 验证

`python -m pytest packages/story-miner/tests/test_knowledge_outputs.py::test_knowledge_index_ingests_miner_outputs -v` -> PASSED。

---

## 最终验收

```
$ python -m pytest packages/knowledge/tests packages/story-miner/tests -q
...................                                                      [100%]
19 passed in 1.49s
```

| 子包 | passed |
|---|---|
| packages/knowledge | 7 |
| packages/story-miner | 12 |
| **合计** | **19 passed, 0 failed** |

另验证三种调用模式均绿：
- 仓库根组合跑（顶层 ini）：19 passed
- `packages/knowledge/tests` 单独（子包 ini）：7 passed
- `packages/story-miner/tests` 单独（子包 ini）：12 passed

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `pyproject.toml`（顶层） | + `pythonpath` 段（Fix A） |
| `packages/knowledge/pyproject.toml` | + `pythonpath = ["src"]`（Fix B） |
| `packages/story-lifecycle/pyproject.toml` | + `pythonpath = ["src"]`（Fix B） |
| `packages/story-miner/pyproject.toml` | + `pythonpath = ["scripts", ".", "../knowledge/src"]`（Fix B） |

约束遵守：未改任何测试断言、未改测试意图；未改 generator / failure_mode / generate_playbooks 实现。
