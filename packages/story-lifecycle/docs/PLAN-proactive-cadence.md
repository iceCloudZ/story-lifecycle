# PLAN: 主动节律 — 每日 TAPD 同步 + 日程简报(story-daily)

> **自包含声明**:在**新窗口**里只读本文即可执行,不依赖任何此前对话。涉及两个系统,所有路径用绝对路径。动手前**先读**这两份具有约束力的操作规则:
> - `D:\github\story-lifecycle\AGENTS.md`(story-lifecycle 总规则)
> - `C:\Users\zzh58\OneDrive\LifeOS\工作\个人项目\定时任务系统\AGENTS.md`(定时任务系统操作规则,冲突时以它为准)
>
> **状态**:待执行 · **日期**:2026-08-14

---

## 0. 这是要解决什么(一句话)

story-lifecycle 现在是**被动**的:你得手动 `story sync` 拉 TAPD、手动开 `story calendar` 看截止日。系统甚至自己承认不自动同步(`list_cmd.py:285` 打印「TAPD 状态未自动同步」)。

**本方案让系统每天主动**:自动拉你的 TAPD 待办→刷新/新建 story,再生成一份「今日该干什么」简报,经现有定时任务系统的看板 + 弹窗推到你眼前。**这就是用户要的"智能/主动感"的根。**

## 1. 不做什么(范围边界)

- ❌ 不碰 agent loop / 不碰 dsh / 不碰半自动 spawn(那是另一条线)。
- ❌ Phase 1 不做 TAPD 状态**回写**(回写能力 `sync_status` 已存在,见 §3;接线到 story 终态转换是 Phase 2,本文末列)。
- ❌ 不改定时任务系统的 `runner.py` / `server.py` / launcher(通用件,新任务只靠 `tasks.json` + 产出前缀接入)。

## 2. 两个系统是什么(自包含背景)

### 2.1 story-lifecycle
- 位置:`D:\github\story-lifecycle`(monorepo),核心包 `packages\story-lifecycle`。
- 安装:editable 装在 venv `D:\github\story-lifecycle\.venv-monorepo-test`,装后提供 `story` 命令 → `D:\github\story-lifecycle\.venv-monorepo-test\Scripts\story.exe`。**前置检查**:`<venv>\Scripts\story.exe --help` 能跑。
- 配置:`~/.story-lifecycle/config.yaml` 的 `tapd:` 段(`workspace_id`、`owner`)。
- DB:SQLite + **raw SQL**(`infra/db/models.py` 是唯一写入/查询通道,无 ORM)。story 行带列:`tapd_status`、`tapd_type`、`tapd_url`、`deadline`、`source_type`、`source_id`、`status`、`lifecycle_state`、`current_stage`、`created_at`(具体列名**以 `models.py` 为准,动手前核对**)。

### 2.2 定时任务系统
- 位置:`C:\Users\zzh58\OneDrive\LifeOS\工作\个人项目\定时任务系统`。
- 机制:`runner.py` 读 `tasks.json` → 到点执行 → 回写 `last_run`。Windows 计划任务 `LifeOS-TaskRunner` **每 30 分钟**触发 runner(daily 任务误差 ≤30 分钟)。
- **加任务 = 在 `tasks.json` 数组加一条**(唯一注册入口),字段:`name`(唯一)/`command`(参数列表,首元素用 python 全路径)/`schedule`(`{"type":"daily","time":"HH:MM"}` 或 `{"type":"interval_minutes","minutes":N}`)/`enabled`/`last_run:null`。`cwd` 字段可选。
- **python 全路径**:`C:\Users\zzh58\AppData\Local\Programs\Python\Python312\python.exe`(计划任务 PATH 不可靠)。
- **脚本约束**:纯标准库优先;第三方包**先问用户**装哪个环境。脚本开头必加 utf-8 reconfigure(计划任务是 GBK)。**产出一律写 `output/`,文件名以任务名开头**(server.py 按前缀关联卡片↔产出)。单任务超时 1h。
- **跨系统任务 = 薄封装**:`scripts/` 下只写「subprocess 调用 + 结果格式化」,业务逻辑留在目标系统。范本:`scripts/patrol_hcall.py`。
- **推送链路**:runner 跑完 → `run_with_notify.py` → `notify.py`(tkinter 弹窗,可点看板链接,60s 自关)。看板 `server.py` 在 `127.0.0.1:8765`。
- **daily 时间不卡整点**,错开几分钟,且与已有任务(08:57 token-usage / 09:17 hcall-patrol)错开。

## 3. 现状盘点(别重建这些)

| 能力 | 在哪 | 备注 |
|---|---|---|
| 拉 TAPD 需求/缺陷(按 owner 的 `custom_field_25` + 待处理态过滤) | `sourcing/sources/tapd_source.py` `TapdSource.fetch_pending()` | `fetch_all=True` 忽略过滤 |
| 拉单个 / 详情 | `TapdSource.get_detail(id)` | |
| **回写 TAPD 状态**(已完成) | `TapdSource.sync_status(item_id, status)` | 映射 completed→done / paused→reopen / failed→postponed。**能力在,只是没接自动流(Phase 2)** |
| `story sync` CLI(拉取→建/更 story) | `entry/cli/sync_cmd.py` | 选项 `--status-only` / `--dry-run` / `--all` / `--id` / `--workspace`(必填,绝对路径) |
| 实际落库 | `orchestrator/service/sync_service.py` `sync_tapd(items, workspace, dry_run, status_only)` → `{created,updated,skipped}` | |
| `story calendar`(按 deadline 分组) | `entry/cli/calendar_cmd.py` | 人读的 rich 表格,非结构化 |
| story 带 deadline/tapd_status 字段 | `list_cmd.py` / `calendar_cmd.py` 已用 | deadline 已过期红/临近黄高亮 |

## 4. 缺口(精确)

1. **没有任何定时器跑 TAPD 同步**——`story sync` 只能手动敲。
2. **没有结构化「今日简报」**——`story calendar` 是人读表格,没法被脚本消费产出"今日该干啥"。
3. **没有自动推送**——你得自己开 8180 或敲 `story calendar`。

## 5. 设计(两部分,业务逻辑留 story-lifecycle,定时任务系统只放薄封装)

### Part A — story-lifecycle:新增 `story daily` 命令(纯读聚合,无副作用)

**新文件** `packages\story-lifecycle\src\story_lifecycle\entry\cli\daily_cmd.py`,仿 `calendar_cmd.py` / `sync_cmd.py` 模式。**注册** 到 `entry/cli/main.py`(参照 `main.py:500-502` 加 calendar 的写法)。

**职责**:从 DB 聚合(raw SQL via `models.py`),输出「今日简报」。两个输出模式:
- `story daily --json`:结构化 JSON(脚本消费)。
- `story daily --md`:Markdown(人读,默认)。

**聚合维度**(每条带 story_key、标题、deadline、tapd_status、lifecycle_state、current_stage、tapd_url):
- 🔴 **已过期未完成**:`deadline < today` 且非终态。
- 🟠 **今日/明日到期**:`deadline ∈ {today, today+1}`。
- 🆕 **今日新落(自上次同步)**:`created_at` 在今日(列名核对 `models.py`)。
- ⛔ **受阻/卡住**:`status` 为 paused / escalate,或 stuck 标记。
- ▶️ **进行中**:active 的并行 story + 各自 current_stage。

**命令骨架**(列名以 `models.py` 为准,先核对再写 SQL):
```python
@click.command("daily")
@click.option("--json", "as_json", is_flag=True, help="输出结构化 JSON")
@click.option("--days", default=2, help="前瞻天数(今日+N)")
def daily_cmd(as_json, days):
    """每日简报 — 聚合今日到期/过期/新落/受阻的 story。纯读,无副作用。"""
    from ...infra.db import models as db
    # 1. raw SQL 查上述 5 类(参照 calendar_cmd._load_stories_with_deadlines 的查法)
    # 2. 组装成 dict(structured)
    # 3. as_json → print(json.dumps(..., ensure_ascii=False, indent=2))
    #    否则 → print Markdown(分节 + 每条一行带 tapd_url)
```

> **注意**:Part A 是**纯新增**,不改 `sync_cmd` / `calendar_cmd`,不影响现有功能。可独立 `story daily` 手测。

### Part B — 定时任务系统:薄封装 wrapper + tasks.json 注册

**新文件** `C:\Users\zzh58\OneDrive\LifeOS\工作\个人项目\定时任务系统\scripts\story_daily.py`(纯标准库,薄封装)。

**职责**:subprocess 调 story-lifecycle 的 `story` 命令 → 把简报落 `output/story-daily-sync-*`。

**骨架**:
```python
# -*- coding: utf-8 -*-
import sys, subprocess, datetime, pathlib
for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError): pass

STORY = r"D:\github\story-lifecycle\.venv-monorepo-test\Scripts\story.exe"
WORKTREES = r"D:\worktrees"          # = config worktrees_root,新 story 落这
OUT = pathlib.Path(__file__).resolve().parent.parent / "output"
OUT.mkdir(exist_ok=True)
today = datetime.date.today().isoformat()
brief_path = OUT / f"story-daily-sync-briefing-{today}.md"

def run(args):
    return subprocess.run([STORY, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")

# 1) 主动拉 TAPD(新需求自动成 story + 刷新状态)
sync = run(["sync", "-w", WORKTREES])      # fetch_pending 已按 owner+待处理过滤
# 2) 聚合今日简报
brief = run(["daily", "--md"])

# 3) 拼产出(哪怕 sync 失败也产出一份带状态说明的简报,别让进程崩)
body = []
body.append(f"# Story 每日简报 · {today}\n")
body.append("## TAPD 同步\n")
body.append(f"```\n{sync.stdout or sync.stderr}\n```" if sync.returncode == 0
            else f"⚠️ 同步失败(退出码 {sync.returncode}):\n```\n{sync.stderr}\n```\n")
body.append("\n## 今日简报\n")
body.append(brief.stdout if brief.returncode == 0
            else f"⚠️ 简报生成失败:\n```\n{brief.stderr}\n```")
brief_path.write_text("\n".join(body), encoding="utf-8")
print(f"产出: {brief_path}")
```

**tasks.json 加一条**(放数组末尾,逗号别漏):
```json
{
  "name": "story-daily-sync",
  "command": [
    "C:\\Users\\zzh58\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
    "scripts/story_daily.py"
  ],
  "schedule": {"type": "daily", "time": "09:07"},
  "enabled": true,
  "last_run": null
}
```
> 时间 **09:07**:错开 08:57(token-usage)、09:17(hcall-patrol),且不卡整点(±30min 误差)。

## 6. 推送面(默认走现有机制,零新增基础设施)

- runner 跑完 `story-daily-sync` → `run_with_notify.py` → `notify.py` **自动弹窗**(可点看板)。
- 看板 `server.py`(:8765)首页出现 `story-daily-sync` 卡片 + 产出链接(`story-daily-sync-briefing-*.md`,前缀匹配 → 卡片可见)。
- → **不用碰 notify.py / server.py**。Phase 1 的推送就靠这条现成链路。

## 7. 逐步实施 + 验证

**Part A(story-lifecycle 侧):**
1. 核对 `infra/db/models.py` 列名(deadline / status / lifecycle_state / current_stage / created_at / tapd_status)。
2. 写 `daily_cmd.py`,在 `main.py` 注册。
3. 手测:`<venv>\Scripts\story.exe daily --md` 和 `--json`,确认五类聚合输出正确、空 story 时不崩。

**Part B(定时任务系统侧):**
4. 确认 `<venv>\Scripts\story.exe` 可用 + `~/.story-lifecycle/config.yaml` 有 `tapd` 段。
5. 写 `scripts/story_daily.py`,**本地直跑** `python scripts/story_daily.py` 确认产出落到 `output/story-daily-sync-briefing-<date>.md`。
6. 加 `tasks.json` 条目。
7. **现场验证(定时任务系统 rule 3,不验证不许说"已注册")**:
   ```bash
   cd "C:/Users/zzh58/OneDrive/LifeOS/工作/个人项目/定时任务系统"
   python runner.py --now story-daily-sync
   ```
   确认:① 退出码 0;② 产出在 `output/story-daily-sync-*`;③ `logs/story-daily-sync-<日期>.log` 无异常;④ `python runner.py --list` 的 last_run 已回写。
8. 看板在跑的话,刷新 `http://127.0.0.1:8765/` 看到新卡片 + 产出链接(.md 渲染成表格/分节)。
9. **回写文档**(rule 8):更新定时任务系统 `README.md`「已注册任务」表加一行。

## 8. 已知坑(逐条对应定时任务系统 AGENTS.md)

- **GBK 编码**:wrapper 开头必须 utf-8 reconfigure(已写进骨架)。
- **venv python vs 系统 python**:`tasks.json` 的 `command[0]` 用**系统 python**(runner 要求),wrapper 内部再 subprocess 调 **venv 的 story.exe**——别让系统 python 直接 import story-lifecycle(第三方包不在系统环境)。
- **`story sync` 强制 `--workspace`**:无论是否 `--status-only` 都校验(见 `sync_cmd.py:77-82`)。本方案用全量 `sync -w D:/worktrees`(顺带把新需求主动建成 story,正是要的"主动接单")。若你只想要状态刷新不要新建,需给 `sync_cmd` 加 `--status-only` 豁免 workspace 校验——**这是可选改动,Phase 1 不做**。
- **产出文件名前缀**:必须 `story-daily-sync-*`(任务名前缀),否则看板卡片看不到链接(骨架已遵守)。
- **daily 时间误差 ±30min**:09:07 实际可能在 09:00–09:30 间跑,正常。
- **TAPD 不可用**:wrapper 要 catch(`sync.returncode != 0` 时仍产出带「⚠️ 同步失败」的简报,别崩)(骨架已处理)。
- **fetch_pending 只拉你的待办**:已按 owner 的 `custom_field_25` + 待处理态过滤,不会把全公司的需求灌进来。

## 9. 开放选择(执行时定,不阻塞)

- **简报要不要更主动地"建议先做哪个"**:Phase 1 只列事实(到期/受阻/进行中);Phase 2 可加优先级排序(按 deadline 紧迫 + 是否阻塞他人)。
- **弹窗要不要显示简报摘要**:Phase 1 弹窗只提示"完成,点看板";若想在弹窗里直接看 Top 3,改 `notify.py`(Phase 2)。
- **要不要接企业微信/钉钉推送**:Phase 1 不做(本地弹窗 + 看板够用)。

## 10. Phase 2(本文不做,仅登记)

- **TAPD 状态回写**:story 到终态(completed/failed)时自动调 `TapdSource.sync_status()` 写回 TAPD——干掉 `list_cmd.py:285` 那句"请手动到 TAPD 更新状态"。接线点:story 终态转换处(state_machine.py 的 mark_completed/mark_failed 之后)。需注意双向同步冲突(本地先变 vs TAPD 先变)。
- **简报优先级排序 + 弹窗摘要**(见 §9)。

## 11. 验收清单

- [ ] `story daily --md` / `--json` 手测通过,五类聚合正确,空数据不崩。
- [ ] `story_daily.py` 本地直跑产出 `output/story-daily-sync-briefing-<date>.md`。
- [ ] `python runner.py --now story-daily-sync` 退出码 0、产出落地、日志无异常、last_run 回写。
- [ ] 看板首页出现 `story-daily-sync` 卡片 + 可点产出链接。
- [ ] 定时任务系统 `README.md`「已注册任务」表已更新。
- [ ] (故意制造 TAPD 配置缺失/断网)wrapper 仍产出带「⚠️ 同步失败」的简报,不崩。
