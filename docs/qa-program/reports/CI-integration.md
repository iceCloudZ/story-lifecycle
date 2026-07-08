# CI 接入与转绿收尾

> 日期:2026-07-08
> 状态:[已完成] · CI 全套首次转绿(success)
> 关联:T1.6(CI 接入)· T5.1(coverage)· T5.2(invariants)

## 做了什么(What)

QA 第一轮跑完后接入 CI,让回归网持续生效。过程中发现 CI 从一开始就全红
(install 步骤失败),逐层剥开 6 层失败,修了 3 个被红 CI 掩盖的真 bug,最终
全套 9 个 job 首次全绿。

## CI 现状(接入后)

```yaml
lint → invariants(架构红线门禁) → test(3 OS × 2 Python)+ coverage → build
```

9 个 job:Lint / Architecture invariants / Test × 6 矩阵 / Verify build。

- **invariants job**(新):跑 `tests/invariants/`,PR 破 6 条架构红线之一就红。`needs: lint` 省额度。
- **coverage**(新):test job 的 ubuntu/3.12 跑 coverage 上传 artifact。**暂不设 fail 阈值**(基线 56.1%,目标 80%,一上来就红没意义)。
- **全平台**:Linux / macOS / Windows × Python 3.10 / 3.12。

## 让 CI 转绿的全过程(6 层洋葱)

CI 之前一直红,但每层失败都**掩盖下一层**——install 死就跑不到 lint,lint 死就跑不到 test。
这次逐层修透:

| 层 | 失败现象 | 根因 | 修复 commit |
|---|---|---|---|
| ① install | `pip install -e ".[dev]"` hatchling 报 "no directory matches dev_flywheel" | 根 pyproject `packages=[]` 不可构建(workspace 容器不是包) | `d7cf1a1e` CI 改直接装 dev 依赖 |
| ② lint | ruff 18 errors(F821/F401/E401)+ format 37 files | 存量 lint 债,被 install 失败一直掩盖 | `fb9d46e4` 修 json 真 bug + 清 F401 + format |
| ③ test collection | `ModuleNotFoundError: story_lifecycle.cli` | ISS-012 把 cli/ 移到 entry/cli/,contract 测试没跟 | `443b7226` 改 import 路径 |
| ④ contract test | `assert_called_once` Called 0 times | 测试没 patch `_MINER_RETROSPECT_SCRIPT`,os.path.exists 守卫跳过 | `c13b4c99` monkeypatch stub |
| ⑤ smoke test | profiles 一致性断言 root=[] vs pkg=[6] | ISS-012(`f0f20baa`)故意删根 profiles,测试过时 | `c13b4c99` 删过时测试 |
| ⑥ Windows test | `UnicodeEncodeError: 'charmap' cp1252` | clarify_server stdout 非 UTF-8 输出中文 | `53547a1d` reconfigure UTF-8 |

## 修了 3 个真 bug(被红 CI 掩盖的)

| bug | 严重度 | 位置 | 说明 |
|---|---|---|---|
| **`api_clarify_stream` 缺 import json** | 高 | `api.py:3083` | SSE 澄清流一触发就 NameError。design HITL 那条线的关键端点,运行即崩 |
| **`clarify_server` Windows 编码崩** | 中 | `clarify_server.py:240` | Windows 上 MCP clarify 工具完全不工作(cp1252 写中文崩) |
| **judge rework 无 max_retries 兜底** | 中 | `gate.py:229` | 理论上能无限 retry。T1.6 早一轮修了 |

**关键认知**:前两个真 bug 被 install 失败掩盖,一直没被发现——这正是 CI 不绿的真实代价:
**真 bug 藏在红 CI 后面**。CI 不修绿,这些 bug 永远不会暴露。

## 代码改动(Changes)

| 文件 | 改动 | 说明 |
|---|---|---|
| `.github/workflows/ci.yml` | 修改 | 3 处 install 改直接装 dev 依赖;加 invariants job;test job 加 coverage |
| `pyproject.toml` | 修改 | packages=[] 加注释说明 workspace 容器意图 |
| `orchestrator/mcp/clarify_server.py` | 修改 | run_server 开头 stdout/stderr reconfigure UTF-8(修 Windows 编码) |
| `orchestrator/service/api.py` | 修改 | api_clarify_stream 补 import json(修 SSE NameError);清 pushed/t0 残留 |
| `orchestrator/engine/planner.py` | 修改 | 删 t0 未用计时残留 |
| `entry/cli/setup.py` | 修改 | 加回 ruff --fix 误删的 re-export(CONFIG_DIR/_merge_config/Path,加 noqa) |
| `tests/contracts/test_done_retrospect_contract.py` | 修改 | import 路径改 entry/cli;monkeypatch stub 修路径解析 |
| `tests/test_smoke.py` | 修改 | 删过时的 test_packaged_and_root_profiles_consistent |
| 37 个 src 文件 | format | ruff format 存量格式化(纯空格/换行) |

最终 commit:`53547a1d`(CI 全绿)。

## 验收结果

**最终 CI run**(`53547a1d`):conclusion=**success**,9/9 job 全绿。
```
✅ Lint & type check
✅ Architecture invariants
✅ Test × 6(3 OS × 2 Python)
✅ Verify build
```
941 passed(每矩阵)。

## 发现/遗留(Findings)

- **[已完成 · 本轮]** 3 个真 bug 修完,CI 首次转绿。
- **[已修] 根 pyproject 不可构建**:`packages=[]` 让 hatchling 报错。CI 绕过(直接装 dev 依赖)。
  根 pyproject 本身未改语义(仍是 workspace 容器)。本地 `pip install -e ".[dev]"` 仍会失败——
  这是 AGENTS.md Setup 指引的一个已知不一致,但本地用户装包走 `packages/<pkg>/pyproject.toml`
  不受影响。
- **[教训] ruff --fix 的 monkeypatch 陷阱**:setup.py 的 `CONFIG_DIR`/`_merge_config`/`Path`
  被测试用 `monkeypatch.setattr` 引用,但 ruff F401 认为它们"未用"误删。加了 `# noqa: F401`
  标注 re-export。以后跑 ruff --fix 后必跑全量测试确认。

## 交接 NOTE

- **本轮收工**:CI 接入完成,全套转绿。QA Program 第一轮(含 CI)彻底收官。
- **后续可选**:
  - coverage 阈值:基线 56.1%,提到 80% 后可在 ci.yml 加 `--cov-fail-under=80`。
  - 根 pyproject editable install:若要本地 `pip install -e ".[dev]"` 也工作,需给根一个
    合法 wheel 目标(产品决策,当前用 CI 绕过)。
- **本地习惯**:改架构相关代码前先 `pytest packages/story-lifecycle/tests/invariants/`。
