# eval 迭代循环任务：UI 驱动端到端验证（round 3）

> 自包含任务文档。执行者：opencode。写于 2026-08-05，前置：round 2 全管线回放（20 样本已在沙箱跑完，数据在 `packages/eval/sandbox/`）。
> 目的：用回放样本驱动**真实 UI + 真实 server** 跑 story-lifecycle，抓编程回放结构上抓不到的 bug：前端渲染/API 契约/交互流程/状态可见性。

## 0. 架构事实（已探明）

- server：`story serve` 起 FastAPI（默认 127.0.0.1:8180，app 在 `orchestrator/service/api.py`，约 122 端点）；前端 `entry/web/` Vue3 静态页。
- story.db 路径可用 `STORY_HOME` 环境变量改指；证据目录随 workspace 启发式（沙箱放 `AGENTS.md` 截断）。
- round 2 回放产物：20 个 story 已在 `sandbox/story_home/` 的 DB 副本里（含 PRD/spec/gate 结果/llm_trace）。

## 1. 铁律

- **端口用 8181**（本机 8180 可能有真服务，严禁占用/停掉）。
- 读路径验证复用 round 2 沙箱 DB（`STORY_HOME=packages/eval/sandbox/story_home` 起 8181 server，只读操作）；写路径用独立沙箱 `sandbox-ui/`（新建 `story_home` + `ws/`，各 ws 放 `AGENTS.md`）。
- 禁止：写原 story.db、写 `D:/hc-all/story/`、TAPD 写操作、git 变更、钉钉外发；hc-all 只读。
- LLM 一律 opencode-go；Build 阶段摘除（同 round 2 profile 思路）。
- 不得修改 story-lifecycle 核心包；Playwright 脚本写 `packages/eval/src/eval/ui_e2e/`。
- Playwright 安装：在 `.venv-monorepo-test` 里 `pip install playwright` + `playwright install chromium`，不装全局、不装系统级依赖。

## 2. 第一部分：读路径验证（零 LLM 成本）

用 round 2 沙箱 DB 起 8181 server，Playwright 逐 story 打开 UI，对 20 个已回放 story 检查（按 UI 实际页面结构调整选择器，先探明页面结构再写断言）：

1. story 列表页：20 个 replay story 全部可见，状态字段非空、无 undefined/NaN/[object Object]；
2. story 详情页：stage 时间线/状态徽章与实际 DB 状态一致（抽查 5 个与 DB 对账）；
3. 文档查看：PRD.md / spec.md 能打开渲染（markdown 正常、非裸文本、非空白）；每类（A/B/C/D）至少 1 个；
4. gate 结果展示：B 类 5 个 story 的 gate verdict/findings 完整可见，findings 无截断乱码；
5. llm_trace / 事件日志页（如有）：能打开、数据非空。
- 每项检查截图存 `results/ui_e2e_shots_20260805/`，失败项同时抓浏览器 console 错误和 server 日志片段。

## 3. 第二部分：写路径验证（2-3 条，真实跑管线）

独立 `sandbox-ui/`，从 UI 完成完整创建流程（先探明 UI 创建 story 的入口形态：表单/PRD 粘贴/上传；若 UI 只支持 TAPD 源创建，记录该限制并用 API 创建 + UI 监控作降级方案，报告中注明）：

1. A 类 1 条 + B 类 1 条（可选 C 类 1 条）：通过 UI 创建 story（用 round 2 的 gold PRD），触发管线；
2. 管线执行期间每 30s 截图 + 记录 stage 状态变化：进度是否实时可见、有无长时间无反馈的假死态；
3. 完成/拦截后：A 类断言 UI 显示通过；B 类断言 UI 显示 gate 拦截且 findings 可见、retry 入口存在且可用（点一次 retry，确认有响应，不必跑完）；
4. 全程收集：浏览器 console 错误、网络 4xx/5xx、server 异常堆栈。

## 4. 产出

`results/ui_e2e_20260805.md`：

1. 检查项通过率（读路径 X/Y，写路径逐条记录）；
2. **bug 清单**：每条含严重度（阻断/功能/体验）、页面、现象、复现步骤、截图路径、console/server 证据——这是本任务的核心产出；
3. UI 能力缺口记录（如「UI 不支持直接粘贴 PRD 创建」类限制）；
4. 沙箱审计：原 story.db md5 前后一致、8180 未被动过、`D:/hc-all/story/` 零写入、hc-all git status 无变化；结束后关掉 8181 server。

## 5. 验收（完成定义）

1. 报告落盘，读路径 20 story 检查全覆盖，写路径 ≥2 条完整记录；
2. 每个失败项都有截图 + 错误证据，可复现；
3. 沙箱审计四项全过；`git status` 核心包零改动；
4. 回复给出：通过率、bug 清单 top（按严重度）、UI 能力缺口、报告路径。

## 6. 后续（不在本任务内）

bug 清单与 round 2 缺陷清单合并分诊，共同构成 story-lifecycle 迭代 1 的改动输入。改动后用同一套 Playwright 脚本回归——脚本即测试资产，每轮迭代复用。
