# Serve 健壮性调查 — 2026-08-11 夜(自主推进轮)

> 用户睡前授权:"这轮你主动往下推进吧。有问题你决定,可以上网查,把决定记录下来,过6个小时找我汇报。"
> 本文档记录此轮的**决定**、依据、执行与结果,供 6h 后汇报。
>
> **关联**:本轨是 **path B(serve 驱动)eval**。eval 方法论/数据地基/judge 三元组/测试金字塔/
> 三轮修复(含迭代3「裁判统一」)的全貌见 `docs/eval-journey-20260804-20260812.md`(kimi+opencode 轨,
> 即 B 线 path A 的完整记录)。两轨**合并点 = 101 服务器**(见文末「与 eval-journey 合并」)。

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

### ⚠️ 重大更正(2026-08-12 ~00:15)— SIGHUP 诊断错了

multirun2 跑到 design→implement 转换时 serve **又崩了**,而且是 **setsid 脱离终端状态下崩的**(PID 438824,SID=自身 PID=新会话领导)。serve-repro.log 精确停在 `approve design → next stage implement`(跟 18:16 原始崩**同一行**),无 traceback / 无 dmesg / 无线程异常。→ **不是 SIGHUP**。之前"setsid 存活 49min = SIGHUP 修复成功"是**假阳性** —— 那 49min 里没有 story 走到 implement,直到 62963 在 23:56 走到,立刻崩。

排除法(全有据):不是 OOM(dmesg 净 24 天 + cgroup 无内存上限 + oom_kill=0)、不是 segfault(dmesg)、不是线程异常(插桩 0 行)、不是 `os._exit`(orchestrator 层 grep 无)。

两个关键洞察:
1. **stderr 重定向到文件是块缓冲** —— 进程若被 SIGKILL,缓冲区输出丢失,"最后一行 = handler log" 可能是假象(后续输出没 flush)。
2. **eval 在 approve 时刻 DELETE story,掩盖了 implement 推进** —— 62963 是旧 multirun 的孤儿(没 eval 删),serve 自由推进到 implement 才崩;正常 eval 轮在 approve 时删 story,把崩点藏起来了。

**自主决定 D6**(进行中):重启 serve 带 `PYTHONUNBUFFERED=1 + PYTHONFAULTHANDLER=1`(冲掉缓冲、抓 segfault/abort)+ patch eval 的 delete 成 no-op,逼 serve 自由推进到 design→implement,抓真正的死因。`b03ae33d`(SIGHUP SIG_IGN)本身没错(SIGHUP 免疫是好防御),但**不是这个崩的解药** —— 真因待 D6 抓到。

#### D6 结果(2026-08-12 ~00:32)— 铁证:是 SIGKILL,3/3 同一崩点

serve-repro2.log(UNBUFFERED)精确停在 `00:20:15 approve design → next stage implement`,**其后全空**:无 traceback、无 "Fatal Python error"、无 faulthandler dump、无 "Shutting down"、dmesg 净 24 天。

**UNBUFFERED + FAULTHANDLER 双静默 = 只能是 SIGKILL**(唯一无法被进程捕获/记录的信号;SIGTERM 会被 uvicorn 记 "Shutting down")。3 次复现(18:16 / 23:56 / 00:20)都精确在 design→implement 转换瞬间,setsid 与否无关 → **彻底排除 SIGHUP**。

**排除清单(全有据)**:非 OOM(dmesg 净 + serve 的 user@1000 cgroup `memory.max=max` + 全系统 `oom_kill=0`)、非 logind(`KillUserProcesses=no`)、非云镜(YunJing 日志无查杀)、非用户 cron(时点不符)、非 stock-collector 的 kill 脚本(不同 PID/port 8000)、journalctl 静默无记录、auditd 未装。

**剩余可能**:① 内存峰值极端情况(swap 耗尽来不及写 dmesg?——但 oom_kill=0 不支持);② 某个不记日志的用户态/内核态 SIGKILL。

**自主决定 D7**:restart serve + 2s 内存采样器 + 不删 eval,**抓死亡瞬间的内存值**(涨到顶=内存因→kill_headless 是解药;平的=外部 SIGKILL→建议挪离这台云 VM 或上 auditd)。

#### D7 结果(2026-08-12 ~00:55)— 内存彻底排除,4/4 同崩点

第 4 次复现(UI-1064584-66148):serve-repro3.log 精确停在 `00:46:08 approve design → next stage implement`,死。**死亡瞬间内存 used≈1200MB / avail≈2500MB(峰值全程才 1295MB)**,dmesg 净,faulthandler 静默。→ **4/4 都在 design→implement 转换 SIGKILL,内存毫无压力(1.2GB/3.6GB),彻底排除内存/OOM。**

**关键新线索**:serve 跑在 `session-XXXXX.scope`(ssh 会话 cgroup)里,且该 scope 状态 = `closing`(我的 ssh 断开后 logind 在回收)。`setsid` 只脱离控制终端,**没逃出 session scope**。session scope 被 logind 回收时,里面的进程会被 SIGKILL(且不写 dmesg、不留 traceback —— 完全吻合现象)。3/4/4 死在 implement-advance 的相关性存疑(scope 回收时机随机),但 session-scope 是目前最强嫌疑。

#### 自主决定 D8(进行中)— capstone:systemd-run --user 逃出 session scope

用 `systemd-run --user --unit=story-serve-r4 --service-type=exec` 启动 serve → cgroup = `user@1000.service/app.slice/story-serve-r4.service`(**逃出 session scope**)。再逼 design→implement:
- **存活**(出现 `HEADLESS spawn stage=implement` + 推进)→ session-scope 是真凶,修法 = 用 user service / systemd-run 启动,别用裸 setsid。
- **仍死** → 更深(auditd / 挪 VM)。

#### D8 结果(2026-08-12 ~01:26)— session-scope 也被推翻!5/5 同崩点,上 auditd

capstone-v2:serve 跑在 `user@1000.service/app.slice/story-serve-r5.service`(**彻底逃出 session scope**,确认 cgroup 不是 session-XXXX.scope),PATH 修好(opencode 全路径 spawn)。**仍死于 `01:18:25 approve design → next stage implement`** —— **5/5**(18:16 / 23:56 / 00:20 / 00:46 / 01:18)精确同崩点。内存峰 1261MB(平),dmesg 净。

**彻底排除清单**:SIGHUP(setsid+SIG_IGN)、内存/OOM(峰 1.2GB、dmesg 净、cgroup oom_kill=0)、segfault(faulthandler 生效+静默)、logind session-scope(systemd-run 逃出仍死)、云镜日志(无查杀)、用户 cron(时点不符)、stock-collector kill(不同 PID)。

**剩余唯一可能:某个不写常规日志的用户态/内核态 SIGKILL**(头号嫌疑=腾讯云安全栈 云镜/stargate,但远程查不到)。**已装 auditd + SIGKILL 监听规则**(`-k serve_sigkill`),第 6 次复现时 auditd 会记录发送者的 exe/pid/uid —— 这是远程抓凶手的终极手段。

#### D8 auditd 轮(2026-08-12 ~01:42)— 崩是间歇性的!serve 这轮扛过去了

第 6 次(systemd-run r6 + auditd 在场):**serve 存活**。journald 出现了前 5 次死亡从未有的完整序列:`approve design → next stage implement` → `decision handled` → **`HEADLESS spawn stage=implement`** → story 推进到 `current_stage=implement` / `_completed_stages=['design']` → implement opencode 跑起来(541MB)。audit 0 个 SIGKILL 事件(没被杀)。

**结论修正:崩是间歇性外部 SIGKILL(5 死 1 活),不是确定性代码 bug。** serve 本身能正常穿 design→implement→后续。触发条件未明(非内存/SIGHUP/session-scope/代码异常),5/6 命中率说明有个概率性外部触发。auditd 已盯着,下次再崩(implement→verify 也是转换点)就抓到 exe。

**给用户的实操结论**:① 这不是 story-lifecycle 代码 bug(5 次死 + 1 次活,无 Python/kernel trace);② 4 个代码提交(可见化/泄漏/SIGHUP/看门狗)+ D2 超时修复都有效且已落;③ 真凶待 auditd 下次抓,或查腾讯云控制台「主机安全/云镜」事件;④ 若反复发生影响 eval,考虑挪离这台云 VM(换无安全 agent 的环境)。

#### D8 终局(2026-08-12 ~02:13)— auditd 抓到凶手 = sshd SIGKILL(会话拆除)

auditd 记录的 **462/462 个 SIGKILL 事件全是 `exe=/usr/sbin/sshd`(uid=root)** 发的。样本:`syscall=62(kill) a1=9 success=yes ... comm=sshd exe=/usr/sbin/sshd`。即 **ssh 会话拆除时 sshd 对附着进程发 SIGKILL**(标准 ssh 会话清理行为)。

**根因链**(最终):
- 我用 `ssh 101 'setsid bash -c "exec story serve" &'` 启动 serve —— **`setsid ... &` 没有可靠逃出 sshd 的会话进程树**。ssh 会话(启动 serve 的那条 ssh)断开时,sshd 拆除会话 → SIGKILL 残留进程 → serve 被杀(5 次,时间随会话拆除时机变化 11~54min)。
- **`systemd-run --user` 启动(run 6)把 serve 放进 `user@1000.service/app.slice/...`,彻底脱离任何 sshd 会话树** → sshd 拆会话不再波及 → **存活 45min+,穿过 design→implement,story 推进到 implement 阶段**。修法被 run 6 证明有效。
- SIGKILL 无法被进程捕获/忽略 → `b03ae33d`(SIGHUP SIG_IGN)救不了它(是 SIGKILL 不是 SIGHUP);但"会话/连接相关"的方向是对的,只是信号搞错了(SIGHUP→实为 SIGKILL)。

**最终修法(已验证)**:serve 在服务器上必须用脱离 sshd 会话树的方式启动 —— **`systemd-run --user --unit=story-serve --service-type=exec --setenv=PATH=... bash -c '...story serve...'`**(run 6 用的)+ `loginctl enable-linger ubuntu`(让 user@1000.service 持久,即使所有 ssh 断开)。**不要用裸 `setsid ... &` 或前台 ssh 跑。** tmux/screen 也行( detach 后进程不在 sshd 会话树)。

**留给用户的(已记,等醒)**:
1. ~~是否要我写一个 `story-serve.service`~~ → **已自主决定并部署**(D9,见下)。
2. 4 提交 + 文档要不要 push GitHub。
3. eval pipeline 那一 pile 未提交改动单独 commit。

#### D9(2026-08-12 ~03:14)— 永久 user unit 部署 + 冒烟通过(修法一劳永逸)

r6(临时 systemd-run unit)证明修法有效(存活 1h45min,穿过 design→implement;69268 在 implement 阶段失败是 opencode 写代码没过,**非 serve 崩**)。遂把临时 unit 升级为**永久 `~/.config/systemd/user/story-serve.service`**:
- `Restart=on-failure` + `RestartSec=5`(崩溃 5s 自愈)
- `WantedBy=default.target` + `Linger=yes`(开机自启、全 ssh 登出持久)
- cgroup `user@1000.service/app.slice/story-serve.service`(脱离 sshd 会话树,sshd SIGKILL 不波及)
- `Environment=PATH=...~/.local/bin...`(opencode 可解析)+ source eval.env(OPENCODE_API_KEY)

**冒烟**:eval `--only 1064584` → create/start/confirm 全 200 → design 全路径 spawn → serve 存活。story 75665 在 design(永久 unit 端到端工作)。3h 监控 + 6h cron 会跟进 75665 是否走完 design→implement。

**结论:serve 健壮性问题闭环。** 真凶(sshd 会话拆除 SIGKILL)抓到 + 修法(永久 user unit,脱离 sshd + 自愈 + 持久)部署验证。剩余仅时间绑定的 6h 汇报(cron 05:47)。

#### D10(2026-08-12 ~04:20)— item 1-5 收尾(用户醒后追加要求全做)

1. **kill_headless 服务器实测 → 抓出 + 修了真回归**(commit fbe54b47):editable 部署 e769c3ae 后第一次 DELETE story → kill_headless → kill_tree 的 `killpg(getpgid(child))` 打到 serve 进程组(opencode 继承了 serve 的组)→ **serve 被杀**(每删一 story 就重起,Restart 兜底)。e769c3ae 单测 mock 了 `_kill_headless`,漏了真 kill_tree 的 killpg 行为。**修:`_spawn_headless` 的 Popen 加 `start_new_session=True`**(opencode 进独立进程组,killpg 只杀 opencode+子)。+ POSIX 回归测试(起 start_new_session 子,走真 kill_headless,断言父存活)。部署后实测:`DELETE 200` + opencode 被杀 + **serve 存活 ✓**。
2. **eval 正式跑一轮(eval ui-replay 全 replay_set)**:2/2 story 产 spec、零停滞零异常、零崩溃。judge 数据:hc-order=`{4,2,3}`、hc-limit=`{5,5,4}`(hc-limit spec 质量更高;方差=flash 随机性,非 bug)。POLL_TIMEOUT=1500 + kill_headless 双修后,eval 第一次干净跑通。
3. **启动自检**(commit ac4f37fe):`_run_server` 调 `_in_session_scope()`,命中 `session-*.scope` 就 `log.warning`(会被 sshd SIGKILL,改用 systemd/tmux/linger)。防再踩启动姿势坑。+ 回归测试。
4. **清理服务器残留**:删 auditd SIGKILL 监听规则 + `serve-repro*.log` + `manual_repro.py`。
5. **全流程 design→implement→verify(FULLTEST 不删)**:serve 穿过 design→implement 转换(kill_headless 在 stage-done 触发,**serve 存活 ✓**),但 implement 阶段 story **paused**(opencode/flash 写不出能过 implement 的代码 → gate/escalate → paused),**未到 verify**。这是 serve 逻辑/模型能力,非 serve 崩。**robustness 目标达成(serve 扛住所有 stage 转换 + 多轮 delete)**,但无 story 完成 design→implement→verify→done(implement 能力是 flash 的瓶颈,非 serve 问题)。

**总验证**:serve uptime ~38min+,经历 ~7 个 story(create/delete/design→implement 转换),**零崩溃**,内存健康(774Mi)。三个修复(系统服务脱 sshd + start_new_session/kill_headless + 看门狗)+ POLL_TIMEOUT 全部服务器实测验证。**item 1 抓出的 kill_headless 回归是最有价值发现**(单测漏网,服务器实测才暴露)。

**服务器最终态**:永久 `story-serve.service`(脱 sshd + Restart + linger)+ editable 装 clone(以后 `git pull` 更新)+ kill_headless/看门狗/启动自检 全 live。

#### D11(2026-08-12 ~10:10)— 全流程 eval 闭环(ui-full auto-advance 验证成功)

`eval ui-full --only 1064584` 跑通**完整 design→implement→verify→done**(commit f6358c0b 的 auto-advance):
- design → implement(~10min)→ **auto-advance #1**(检测 `paused @ implement` = 开发→测试 confirm-gate → 自动 POST /lifecycle/advance)→ verify → **终态 `completed`,done=['design','implement','verify']**,advances=1,anomalies=0,1496s。
- 并行的 FULLTEST-77488(手动 advance 的)也 completed(verify judge approve → All stages completed)。
- **两个 story 都跑完全流程**,serve 6.5h 零崩,内存 719Mi。

**结论**:implement 阶段从不失败(你判断对 —— flash 够用);卡的是 lifecycle confirm-gate,auto-advance 续推后全流程闭环。**全流程 eval(path B / serve)现在可全自动跑通**,与 path A(in-process)凑 A vs B 差分(见与 eval-journey 合并方向)。

#### D12(2026-08-12 ~15:00)— prompt 迭代闭环 + 并行轨对齐 + 整体收口

**eval 驱动 prompt 迭代闭环(eval 第一次兑现"改 prompt → 提分")**:
- 5-story path-B eval 测出 `template_compliance` 均值仅 **2.4**(两次得 1)。诊断:design prompt 的
  `build_design_dimensions_section` checklist 给的是 13 个**技术设计维度**,跟 spec-template/judge 要的
  **Release 部署章节**(SQL变更/Nacos/验收测试/验收计划/大表)错位 → opencode 忠实写 13 维度但缺 Release 章节。
- 改 prompt(`0a0d8481`):checklist 末尾补「spec.md 必须包含 Release 章节」段。
- 复测(5 story,path B):`template_compliance` 均值 **2.4 → 3.8(+58%)**、最低 1→3(不再有 1);completeness
  4.2→4.8、acceptability 2.8→3.8 同涨。**直接证据**:改后 spec 真带了 `### SQL 变更/Nacos/验收测试/验收计划/大表`
  全齐(opencode 照新 prompt 写出来了,非 judge 噪音)。注:n=1 前后对比,幅度远超 flash 方差且 spec 结构变化是
  直接证据;要更严谨可多轮平均。

**并行轨(kimi/opencode)对齐情况**(他们在我干活时也提了 commit):
- `c4c2d706` pipeline_replay(path A)也加了 auto-advance —— 跟我 path B 的 ui-full(`f6358c0b`)对齐,**两轨都过 confirm-gate 了**(我在同步提示词提醒过"path A 也得加",他们加了)。
- `0c4f6741` 迭代4 附录:A 线结论(格1放行率 3/15=20%、格2拦截 19/19=100% → 系统性保守,议题2 升级待修);B 线 no_artifacts 根因(短 id `[-7:]` bug + story_refs 钉钉 UI 噪音)+ nightly 验证。
- ⚠️ eval-journey 文档有**非 utf-8 字节(0xa3 @ pos 15951,在并行轨正文里)** —— 我之前 utf-8 追加变乱码已撤回(revert),编码问题在他们那边,建议修。

**整体收口(本会话 path B / serve 轨)**:
- serve 健壮性:崩(sshd SIGKILL)/泄漏(kill_headless+start_new_session)/看门狗/自检/超时 —— 全修,9h+ 零崩。
- path-B harness:ui-replay(design)/ ui-full(全流程 auto-advance)/ diff(A vs B)。
- eval 产出:A vs B 差分(第一份,serve≈in-process)/ 5-story 方差 / **prompt 迭代闭环(template +58%)**。
- 合并:eval-journey ↔ SERVE-ROBUSTNESS 交叉引用 + 101 统一 env(path A+B 同机)+ 同步提示词。

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

1. **git push 未推送提交 + 服务器重装**(让 kill_headless/SIGHUP/看门狗/slug-dir 清理在服务器生效)—— 需明确授权(AGENTS.md:git push 须用户要求)。
2. eval pipeline 那一 pile 未提交改动(ui_replay.py / cli.py / dataset.py 等)是否单独 commit(我没动它们,除 POLL_TIMEOUT 一行)。
3. hc-limit(tapd-7447)judge 低分(4/1/1)—— spec 落地但质量差,是另一个问题(opencode 设计质量 / judge 标准),不在本轮 serve 健壮性范围,记录待查。

### D13 worktree 清理治本 —— slug dir 才是真凶(db855c99,本地已验证,待 push 部署)

**背景**:用户要求"连治本的 delete_story 清 worktree 一起做"。2a42c3e7 加了
`force_cleanup_story_worktrees`(清 git worktree)+ eval-replay `cleanup_worktree_on_delete`
flag + delete_story 接线 + run_dir 时间戳化 + eval cleanup 保留 results。

**exec_73aee449 实测(101 服务器,2a42c3e7 部署后)**:

| 项 | 结果 |
|---|---|
| 部署(git pull + serve 重启) | ✅ serve UP pid=790175 |
| eval cleanup(清 active,留 results) | ✅ "cleared 0 active → failed" + "results/ 成果物保留" |
| run_dir 时间戳化 | ✅ `ui_replay_20260812-154050/`(date-HHMMSS,不覆写) |
| **delete 清 worktree** | ❌ **worktree 目录 0→1(没清干净!)** |

**根因**(Explore agent 定位):agent cwd 的 **workspace slug dir**(`<worktrees_root>/<slug>/`,
`_prepare_story_workspace` 建、存 `context_json.workspace_path`)从来没人回收。
`force_cleanup_story_worktrees` 只清 git worktree(注册在 `story_projects` 的),漏了 slug dir
—— 这才是"eval 跑一轮留一片"的真凶(2a42c3e7 只治了表层的 git worktree)。

**db855c99 修**:force_cleanup 加第二层 —— 读 `context_json.workspace_path` → resolve 后确认
位于 `get_worktrees_root()` 之内(**安全闸**:绝不碰任意路径/主 workspace/repo)→ rmtree。
real-user profile 不开 flag,仍保留 worktree 给 /restore。

**本地验证**(131 passed 0 failed):
- `test_force_cleanup_story_worktrees` 扩展:建真 git worktree + 真 slug dir → force_cleanup →
  `removed>=2` + 两个目录都没了(覆盖两层)。
- 新增 `test_force_cleanup_refuses_paths_outside_worktrees_root`:workspace_path 指 root 外的
  "precious_repo" → 安全闸挡住,不删。

**待办**:~~push db855c99 → 服务器重装 → 重跑验证~~ ✅ 完成(见下"服务器实测闭环")。

### 服务器实测闭环(2026-08-13,push + 部署后)

db855c99 + 9d193b1d(eval cleanup 加 `git worktree prune`)均已 push + 部署到 101。两层清理模型:

| 层 | 谁清 | 清什么 | commit |
|---|---|---|---|
| **磁盘** | `force_cleanup_story_worktrees`(delete_story 调) | slug dir(agent cwd,含其内 agent 建的 worktree 文件)+ 注册在 story_projects 的 git worktree | db855c99 |
| **注册** | `eval cleanup` → `git worktree prune` | delete rmtree slug dir 后,源仓残留的 stale worktree 注册(gitdir 指向已删路径) | 9d193b1d |

**实测两轮覆盖两种 case**:

1. **exec_1d4aa1ef**(agent 建了 in-slug worktree):create→spawn→delete → 目录 **1→0**✓(slug dir 清了),但 git-worktree list 4→5(#5 prunable)。后手动 `git worktree prune -v` → 输出 "Removing worktrees/hc-order: gitdir file points to non-existent location" → **5→4**✓(prune 回收 stale 注册)。
2. **exec_630f82d3**(agent 没建 in-slug worktree):create→spawn→delete → 目录 0、list 1(delete 干净)→ eval cleanup prune 回收 0(正确,无 stale)。

**legacy 清理**:更早轮次(db855c99 前的旧流程,agent cwd 是 repo dir)在 `repos/hc-order/` 下留了 3 个带文件的 worktree(#hc-order/hc-order-66148 等,共 ~13M)→ `git worktree remove --force` 逐个清掉 → list 回到 1(只剩 main)。

**最终态**:目录=0、git-worktree list=1(main)。eval 跑一轮 = 磁盘 0 积累 + 注册 0 积累 + 成果物时间戳保留。**用户原诉求"每次 eval 跑完什么都没留下"+"42 个 worktree 堆积"彻底解决。**

