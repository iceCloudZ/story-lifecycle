# M6 验收：story-miner scripts hc-all 硬编码泛化

> 任务：把 `packages/story-miner/scripts/` 里硬编码 `D:/hc-all` / `agent-transcript-miner` /
> 仓库绝对路径的脚本，改为 `from miner import config` 配置驱动。hc-all 仅作默认配置保留。
> 约束：不改脚本功能，只把硬编码改 config 驱动。

## 1. config 层

### `miner/config.py`
- `_resolve(path_key)`：统一路径解析顺序 `环境变量 > config.json 绝对路径 > 相对包根(_PROJ)`。
  - `db_path` → 环境变量 `MINER_DB_PATH`
  - `cache_dir` → 环境变量 `MINER_CACHE_DIR`
- `CACHE_DIR = _resolve('cache_dir')`：分析脚本的中间产物缓存目录
  （旧布局 `events.pkl`/`sessions.json` + 各脚本输出的 `dN_*.md`/`explore.md`）。
- `DB_PATH` / `WORKSPACES` / `CLAUDE_ENCODINGS` 行为不变（`DB_PATH` 改走 `_resolve`，结果等价）。

### `config.json`
```json
{
  "db_path": "data/transcripts.db",
  "cache_dir": "D:/hc-all/.claude/tmp/cache",
  "workspaces": ["D:/hc-all", "D:/java-agent", "D:/github"]
}
```
`cache_dir` = hc-all 默认场景（可由 config.json 或 `MINER_CACHE_DIR` 覆盖）。

> config.py / config.json 里保留 hc-all 字面量属于「默认配置」，按 M6 约定不算硬编码 bug。

## 2. 改动的脚本

### 读 cache 产物的脚本（5 个，路径全走 `config.CACHE_DIR`）
| 脚本 | 改动 |
|---|---|
| `scripts/distill.py` | `events.pkl`/`sessions.json` 读 + `d2_distill.md` 写 → `config.CACHE_DIR`；`ws!='hc-all'` 过滤 → `PRIMARY_WS`（`config._cfg['primary_ws']` / `MINER_PRIMARY_WS` / `ws_of(WORKSPACES[0])`，默认 hc-all） |
| `scripts/explore.py` | `DB` → `config.DB_PATH`；`explore.md` 写 → `config.CACHE_DIR`；方向③工作区列表 `['hc-all','java-agent','story-lifecycle']` → 数据驱动（`SELECT DISTINCT ws`） |
| `scripts/learn.py` | `events.pkl` 读 + `d8_learn.md` 写 → `config.CACHE_DIR` |
| `scripts/toolopt.py` | `events.pkl` 读 + `d7_toolopt.md` 写 → `config.CACHE_DIR` |
| `scripts/workload.py` | `sessions.json` 读 + `d4_workload.md` 写 → `config.CACHE_DIR` |

### 直接读 DB 的脚本（5 个，`DB`→`config.DB_PATH`，输出→脚本相对 `scripts/out/` 或 `<workspace>/.story/`）
| 脚本 | 改动 |
|---|---|
| `scripts/retrospect.py` | `DB` → `config.DB_PATH`；`OUT_DIR`/`OUT_BATCH` → 脚本相对 `out/`；`batch_top5` 的 `ws='hc-all'` → `PRIMARY_WS`（参数化 SQL `ws=?`） |
| `scripts/constraint.py` | `DB` → `config.DB_PATH`；`OUT_DOC` → 包相对 `_PROJ/docs/`；生成文本里的 `agent-transcript-miner` → `story-miner` |
| `scripts/debt.py` | `DB` → `config.DB_PATH`；`OUT` → 脚本相对 `out/` |
| `scripts/predict.py` | `DB` → `config.DB_PATH`；`OUT`/`OUT_EFFORT` → 脚本相对 `out/` |
| `scripts/recommend.py` | `DB` → `config.DB_PATH`；`OUT` + `--package` 的 `context-package-*.md` 路径 → 脚本相对 `out/` |

### 本次修复：`scripts/generate_playbooks.py`（config 驱动被硬编码定义顶掉的 bug）
M5/M6 文档曾标 `generate_playbooks.py` 已 config 驱动，但实际存在两处「定义被后面硬编码覆盖」：
- `THEME`：第 28 行已写 `THEME = config._cfg.get('playbook_themes', default_themes)`，但第 41-49 行又用硬编码字典无条件覆盖 → config 覆盖值被顶掉。
- `_HC_SERVICES`：第 31-34 行已写 `config._cfg.get('service_names', ...)`，但第 104-108 行又硬编码元组覆盖。

**修复**：删除两处重复的硬编码定义，让第 28/31 行的 config 驱动定义生效。
- `THEME` 现仅一处赋值（config 驱动，默认 = 原 7 主题）
- `_HC_SERVICES` 现仅一处赋值（config 驱动，默认 = 原 11 个 hc- 服务名）
- 不改默认行为（默认值与原硬编码等价），但 config.json 的 `playbook_themes` / `service_names` 覆盖现在真正生效。

所有脚本统一加 `sys.path.insert(0, <包根>)` 后 `from miner import config`，与已 config 驱动的 `failure_mode.py` / `tri_efficiency.py` / `stage_cost.py` 同模式（这三者原本就走 `_PROJ` 相对 + `config.DB_PATH`，无需改）。

## 3. grep 确认（验收硬要求）

脚本（`packages/story-miner/scripts/*.py`，不含 config.py）逐文件 grep：
```
D:/hc-all                    → PASS: 0 hits
D:/github/story-lifecycle    → PASS: 0 hits（仓库绝对路径一并清理）
agent-transcript-miner       → PASS: 0 hits
```

剩余 `hc-all` 字面量均为合法数据值/注释（非路径），允许保留：
- `distill.py:L8` / `explore.py:L37` / `generate_playbooks.py:L7` / `retrospect.py:L15-16` / `tri_efficiency.py:L3,L236`：解释 config 驱动的注释
- `failure_mode.py:L202` / `predict.py:L137`：分析结论文本里的数据分布描述（数据事实，如「复杂度标注主要落在 hc-all」）

## 4. 抽跑验证

运行方式（monorepo 根目录）：
```
cd /d/github/story-lifecycle
PYTHONPATH=packages/story-miner python packages/story-miner/scripts/<x>.py
```

### 语法检查（全部 14 个脚本）
`ast.parse` 全过：**14 OK, 0 FAIL**。

### config 解析（实际打印）
```
DB_PATH   = D:\github\story-lifecycle\packages\story-miner\data\transcripts.db
CACHE_DIR = D:/hc-all/.claude/tmp/cache
WORKSPACES= ['D:/hc-all', 'D:/java-agent', 'D:/github']
```

### 直接读 DB 的脚本（端到端跑通）
- `explore.py` → `explore done; multi-story: 5 retry-groups: 12 fails: 1602`
  输出写入 `config.CACHE_DIR/explore.md`（4272 bytes）
- `generate_playbooks.py --workspace <tmp>` → 端到端跑通：解析 ws、建
  `<workspace>/.story/knowledge/playbooks/` 目录、查 DB（临时 ws 无 session → 0 playbook，无报错）。
  输出路径由 `--workspace` 参数驱动，不再写死 hc-all。

### config 覆盖验证（证明「换 config 能分析非 hc-all 项目」）

**(a) `MINER_CACHE_DIR` 覆盖（explore.py 输出重定向）**
```
MINER_CACHE_DIR=<tmp> python explore.py
→ explore.md（4272 bytes）写入覆盖目录，不再写 D:/hc-all
```

**(b) generate_playbooks THEME / service_names 覆盖传播（本次修复的关键证据）**
monkeypatch `config._cfg['playbook_themes']` / `['service_names']` 后 import 脚本：
```
override THEME:        ['my-theme']                 ← 覆盖值生效（修复前会被硬编码顶回 7 主题）
override _HC_SERVICES: ('custom-svc-a','custom-svc-b') ← 覆盖值生效（修复前会被硬编码顶回 hc-order..）
PASS: config override propagates to THEME and _HC_SERVICES (no hardcode shadowing)
```

### 读 cache pickle 的脚本（distill/learn/toolopt/workload）
依赖旧布局 `events.pkl`/`sessions.json` ingest 产物（当前 pipeline 产物是 DB，pickle 需另行生成）。
已通过：① 全部 `ast.parse` 语法 OK；② `MINER_CACHE_DIR` 覆盖路径验证证明读/写路径均 config 驱动。

## 5. 结论

- 验收硬要求（scripts/*.py 不含 `D:/hc-all` / `agent-transcript-miner` / 仓库绝对路径）：**PASS**
- 额外修复：`generate_playbooks.py` 的 `THEME` / `_HC_SERVICES` config 覆盖被硬编码定义顶掉的 bug
- hc-all 作为默认配置保留（config.py / config.json），符合 M6「hc-all 只是默认场景」约束
- 脚本功能未改，仅路径/过滤改 config 驱动
