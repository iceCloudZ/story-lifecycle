# Serve 健壮性调查 — 2026-08-11 夜(自主推进轮)

> 用户睡前授权:"这轮你主动往下推进吧。有问题你决定,可以上网查,把决定记录下来,过6个小时找我汇报。"
> 本文档记录此轮的**决定**、依据、执行与结果,供 6h 后汇报。

## 背景(已确认的三件事)

经服务器实测(101.34.219.188)+ 代码溯源,定位了三个独立问题:

1. **serve "崩" = SIGHUP**(不是代码线程 bug)
   - 证据:`dmesg` 干净 24 天(排除 OOM/segfault);serve 所有 worker 线程 `daemon=True`(线程异常杀不了进程);serve.log 突死无 traceback 无 "Shutting down";uvicorn 源码确认单进程无 SIGHUP-reload handler → 默认 terminate。
   - 机制:`story serve` 前台 ssh 裸跑 → 断开 → shell 发 SIGHUP → serve 死。

2. **headless opencode 子进程泄漏**(真实代码 bug)
   - `DELETE /api/story/{key}` 只 `kill_pty`,不碰 `_headless_procs`;该注册表全代码库无 pop/del/clear。stage 批准 + story 删除后,opencode(~591MB)继续跑。

3. **"design 停滞"= eval 超时配短了**(非 serve bug)
   - 证据:61970 hc-order 同任务跑 14min 仍在 opencode step 60 活跃干活;60552 同任务 8.6min 完成 → **deepseek-v4-flash 同任务耗时 8~15min 方差**。eval `POLL_TIMEOUT=600s`(10min)< 真实耗时 → 慢轮被误判"停滞"。
   - 插桩(a0abc015)全程静默 → 无线程死,编排正常 poll。

## 本轮自主决定(已记录)

| # | 决定 | 依据 |
|---|---|---|
| D1 | **不 `git push` 到 GitHub**(defer 到用户醒) | AGENTS.md 硬规则"git push 仍需用户明确要求";用户已睡,push 是 outward-facing/难撤销;改用**服务器原地 patch** 推进验证(同插桩方式,已验证可行)。 |
| D2 | **eval `POLL_TIMEOUT` 600→1500s** | 覆盖 flash 设计阶段真实耗时(8~15min 方差),消除伪停滞。这是"停滞"最直接的解药。本地 + 服务器双向改。 |
| D3 | **服务器原地 patch `kill_headless`(e769c3ae)+ SIGHUP(b03ae33d)** | 让运行中的多轮循环享受修复(防泄漏),无需 push/重装。SIGHUP patch 对已运行的 serve 无效(下次重启生效),但现场已 setsid 免疫。 |
| D4 | **judge 随机性 = flash 同任务方差**,非 bug | 不修,靠多轮平均。 |
| D5 | **(stretch)实现 `supervise_headless_stdout` wall-clock 超时** | 防御 opencode 真·卡死(本轮 61970 是慢不是卡,但未来上游 API 真挂时需要兜底)。若时间允许做 + 测试。 |

## 已落地提交(本地 main,全包 pytest 1410 passed)

- `a0abc015` 线程可见化(6 处 `except:pass`→log + `threading.excepthook`)
- `e769c3ae` headless 泄漏修复(`kill_headless` + delete/stage-done/shutdown 三处 + 2 回归测试)
- `b03ae33d` SIGHUP 兜底(`story serve` 自身 `SIG_IGN` + 回归测试)

## 执行计划(本轮)

1. [ ] D2:eval `POLL_TIMEOUT` 600→1500s(本地 + 服务器 patch)
2. [ ] D3:服务器原地 patch `kill_headless` + SIGHUP(4 文件 patcher)
3. [ ] 停掉当前 multirun,装上修复后重启(干净数据)
4. [ ] 多轮跑 ~1h,确认:伪停滞消失(spec 落地率↑)+ 无泄漏(opencode 计数不攒)
5. [ ] D5(stretch):supervise 超时 + 测试
6. [ ] 填本文件"结果"段 + 生成 6h 汇报

## 结果(执行后回填)

### 已完成(2026-08-11 ~23:55)

- **4 个提交全部在本地 main,全包 pytest 1412 passed, 5 skipped**:
  - `a0abc015` 线程可见化(服务器 0.11.6 已原地补丁,全程 0 线程死亡)
  - `e769c3ae` headless 泄漏修复(kill_headless)+ 2 回归
  - `b03ae33d` SIGHUP 兜底(story serve SIG_IGN)+ 回归
  - `54d92bd7` headless 看门狗(_arm_headless_watchdog 超时强杀)+ 2 回归
- **D2 POLL_TIMEOUT 600→1500**:本地 ui_replay.py:34 + 服务器 eval(site-packages)都已 patch,py_compile 过。
- **服务器现场**:serve setsid 脱离终端存活 49min+(SIGHUP 免疫);multirun2(POLL_TIMEOUT=1500 + 迭代间 `pkill opencode` 清泄漏)iter 1 进行中。
- **D1 决定执行**:未 push(AGENTS.md 规则 + 用户已睡);服务器侧用原地 patch + 迭代间清理代替 kill_headless 部署(kill_headless 已 commit + 单测,正经部署等用户 push 重装)。

### multirun2 验证(等数据,6h cron 回填)

预期:POLL_TIMEOUT=1500 下,之前误判"停滞"的慢轮(hc-order 8~14min)应在窗口内 spec 落地 → `spawn=True spec=True`。判据:
- 伪停滞消失 = `spawn=False anomalies=2` 不再出现(或大幅减少)。
- judge_B 分数方差 = 同 story 多轮 completeness/template_compliance/acceptability 的波动(flash 随机性,非 bug)。
- 内存 = 迭代间 pkill 后回落,不单调上涨。

> 6h cron(automation-d3c95b9f)会 ssh 101 读 multirun2.log 最终状态,补本节并汇报用户。

### 需用户拍板的下一步(已记,等醒)

1. **git push 4 提交 + 服务器重装**(让 kill_headless/SIGHUP/看门狗在服务器生效)—— 需明确授权。
2. eval pipeline 那一 pile 未提交改动(ui_replay.py / cli.py / dataset.py 等)是否单独 commit(我没动它们,除 POLL_TIMEOUT 一行)。
3. hc-limit(tapd-7447)judge 低分(4/1/1)—— spec 落地但质量差,是另一个问题(opencode 设计质量 / judge 标准),不在本轮 serve 健壮性范围,记录待查。

