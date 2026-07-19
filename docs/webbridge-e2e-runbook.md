# WebBridge 全 UI 端到端测试 · 执行手册

> **目标**:用真实浏览器(Kimi WebBridge 驱动 Chrome)走完整条用户链路(UI 创建
> story → plan → 推进各 stage gate → clarify → completed),验证 story-lifecycle
> 的**前端 SPA + 真实 HTTP/WS/SSE 网络 + AI 全流程**没被改动破坏。
> **定位**:不是单测的替代,是 in-process e2e(`real-story-e2e-runbook.md`)覆盖不到
> 的"真人视角整条链路"验收。判定权归纯 Python asserters,不交给 AI。
> **起点日期**:2026-07-19。

---

## 0. 它能验收什么(in-process e2e 覆盖不到的)

| 层面 | 单测 | in-process e2e | **WebBridge e2e** |
|---|---|---|---|
| 函数逻辑 | ✅ | — | — |
| API 契约(TestClient) | 部分 | ✅ in-process | ✅ **真实 HTTP/WS/SSE** |
| 前端 SPA 的 intake/gate/clarify UI | ❌ | ❌ | ✅ |
| plan 真生成(不降级 default) | ❌ | 部分 | ✅ |
| AI CLI 真收到 prompt | ❌ | ❌ | ✅(kimi "开了但指令没进去"这种) |
| 整条 stage 推进链路 | ❌ | ✅ in-process | ✅ over-the-wire |
| AI 真能实现需求 | ❌ | ✅ | ✅ |

**最适合的场景**:你改了 `planner` / `infra/terminal`(PTY) / `knowledge/adapters` /
`infra/llm_client` / `orchestrator/engine/graph` 这些核心编排逻辑后,跑一次它,
验证整条用户链路没坏。这次(2026-07)正是靠它抓住并修了 4 个后端 bug。

---

## 1. 环境前置

| 项 | 值 / 要求 |
|---|---|
| story-lifecycle 仓库 | `D:\github\story-lifecycle`(main),已 `pip install -e packages/testing` |
| WebBridge daemon | `127.0.0.1:10086` 在跑 + Chrome/Edge 扩展已连接。自愈:`~/.kimi-webbridge/bin/kimi-webbridge.exe start` |
| AI CLI | `claude`(或 `codex`)在 PATH;profile 里 build 用 `kimi` 也行(已修 readiness_marker) |
| LLM 配置 | `~/.story-lifecycle/config.yaml`(deepseek 等),`load_config_to_env()` 能读 |
| hc-all 工作区(Java 场景) | `D:\hc-all`,其下 `hc-config` 是 Maven 子项目(已注册为工作区) |

健康检查:
```bash
# WebBridge daemon + 扩展
curl -s http://127.0.0.1:10086/status   # running=true, extension_connected=true
# 故事 LLM 已配
story doctor
```

---

## 2. 两个现成场景

| 场景 | 文件 | 语言 | workspace | DB |
|---|---|---|---|---|
| calculator | `tests/e2e/test_calculator_webbridge_e2e.py` | Python(pytest) | scenario 自有目录 | **隔离**(webbridge_server) |
| hc_config | `tests/e2e/test_hc_config_webbridge_e2e.py` | Java(Maven) | `D:\hc-all\hc-config`(真实子项目) | **真实**(real_webbridge_server) |

- calculator:Red→Green 模型,隔离 DB,不碰真实数据。最快验证框架本身。
- hc_config:真实 Java 仓库,微示例需求(WebBridgeDemoUtil 工具类)。`InjectedSpecPrep`
  把 JUnit 测试注入 hc-config,跑完清理(删注入测试 + AI 写的 impl,**不删** `.story/`
  产物——那是判定证据)。

---

## 3. 跑

```bash
# 从仓库根
pytest -m real_web_e2e tests/e2e/test_calculator_webbridge_e2e.py    # Python,隔离
pytest -m real_web_e2e tests/e2e/test_hc_config_webbridge_e2e.py     # Java,真实 hc-all
```

- `real_web_e2e` marker **默认 skip**(不进 CI),必须 `-m` 显式选。
- 跑时**你会看到 Chrome 自动开 tab** —— 就是 WebBridge 在驱动真实浏览器。
- 单次约 **10-15 分钟**(design 4min + build 写代码 + verify),真实 LLM 花钱。

---

## 4. 框架组件速查(`packages/testing/src/testing/web/`)

```
server.py      真实 uvicorn 起停(同进程子线程,绕开 CLI config gate)
               - webbridge_server: 隔离 DB(tmp STORY_HOME),calculator 用
               - real_webbridge_server: 连真实 ~/.story-lifecycle,hc_config 用
webbridge.py   WebBridge daemon 客户端: navigate/snapshot/click/fill
               + find_refs(@e 文本匹配,抗 DOM 变化)
               + click_dom_button(evaluate 兜底,a11y 看不到按钮时)
api_client.py  httpx 镜像后端契约 + SSE/WS(plan_stream / wait_until 轮询)
scenario.py    run_scenario(): 全 UI 编排
               - ui_seed=True: IntakeStartModal 创建(填表/选工作区/选项目/填 PRD)
               - strict_ui: 任何 state-changing 操作不走 API 回退,失败即报错
               - WorkspacePrep / CalculatorPrep / InjectedSpecPrep: 建桩 + 清理
runner.py      可插拔 test runner:
               - MavenTestRunner(-pl <module> -am -DfailIfNoTests=false, maven_root)
               - PytestRunner
judge.py       ScenarioJudge + CalculatorJudge + HcAllJavaJudge
               判定权 100% 在纯 Python asserters(复用 testing.asserters)
```

---

## 5. 已知坑 + 应对(调试时踩过的)

| 坑 | 应对 |
|---|---|
| WebBridge evaluate 长 code 在 SPA 截断(`Unexpected end of input`) | select 用短 code;textarea 用分块 evaluate(120 字符/块) |
| WebBridge fill 对受控 textarea 抛 Uncaught | 用 evaluate 原生 value setter(分块) |
| stage gate 按钮 a11y 时有时无 | `_advance_gate` 先 a11y retry,失败用 `click_dom_button`(DOM click 兜底) |
| paused 有两种:stage gate(`确认推进`)/ execution(`继续执行`) | `_advance_gate` 读后端 `_stage_gate` 区分,选对应按钮 |
| build 的 kimi 静默退出(done 没写) | planner poll 循环加 `_agent_pty.alive` 检查 → failed → rescue |
| **kimi prompt 注入失败**("开了但指令没进去") | ShellAdapter 内置 `readiness_marker`(kimi=`Welcome to Kimi Code`),等启动完再注入 |

---

## 6. 诚实状态(2026-07-19)

- ✅ **端到端验证过**:完整跑(UI intake → plan → design → build → verify →
  completed)实测产出 `WebBridgeDemoUtil.java`(kimi 真写代码),story 到 completed。
- ⚠️ **稳定性未到"必过"**:同一份代码偶发卡在 planning/gate 推进(LLM plan 时机 +
  gate 按钮渲染 + WebBridge snapshot 的非确定时序)。**红了要人判断**——真 regression
  还是 timing flake。
- 🎯 **当前定位**:"改了 planner/PTY/adapter/llm_client 核心后"的手动/nightly 验收工具。
  还不是"红了就是 bug"的硬 CI 门。
- 📝 **判定权在代码**:driver 只驱动 UI,pass/fail 由 `testing.asserters` 断后端产物
  (done 文件存在/非空 + 真实 mvn test 退出 0),不交给 AI。

---

## 7. 加一个新需求的验收场景

1. 写 PRD:`packages/testing/src/testing/scenarios/<your>/{PRD.md}`
2. 写验收测试(断"做对了"):同目录(如 `XxxTest.java` / `test_xxx.py`)
3. 建测试文件(参考 `test_hc_config_webbridge_e2e.py`):
   - `SUBPROJECT` / `MODULE` / `IMPL_PKG` / `CLASS` 指向目标位置
   - `_INJECT`(scenario 源 → workspace 目标)/ `_RED_FILES`(AI 该产出的)
   - `HcAllJavaJudge(subproject=..., module=..., impl_rel_package=..., class_name=...)`
4. `pytest -m real_web_e2e tests/e2e/test_<your>.py`

判定标准(Decider)必须你写——需求对不对,只有人知道。框架只提供"怎么跑 + 怎么断产物"。
