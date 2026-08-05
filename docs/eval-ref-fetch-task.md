# eval 参照物抓取任务（ref-fetch）

> 自包含任务文档。执行者：opencode。目标：把 TAPD 描述里「只有链接」的需求，链接背后的文档内容抓下来落成本地参照物，供 linker / judges 使用。
> 写于 2026-08-05，基于当时的数据快照；数字以实际重算为准。

## 1. 背景与目标

`packages/eval/dataset/tapd_stories.jsonl` 共 6309 条需求，其中 **285 条**的 description 去 HTML 后只剩链接 + 模板占位词（背景/价值/目标/内容），没有可评的需求正文。judge 对这些需求只能给 alignment=1（"无法建立映射"），污染分数；link-mine 也匹配不上。

任务：抽取这些链接，按域名分路线抓取正文，落成 `dataset/story_refs/<tapd_id>.md`，并更新索引。judge/linker 后续读富化后的参照物重评。

## 2. 输入数据与口径

- 需求文件：`D:/github/story-lifecycle/packages/eval/dataset/tapd_stories.jsonl`
  - 字段：`tapd_id, name, status, iteration_id, owner, created, modified, description`（HTML）
- link-only 判定口径（与统计脚本一致，直接复用）：
  1. `desc = 去 HTML 标签(description)`
  2. `text = desc 去掉所有 http(s) URL，再去掉 背景/价值/目标/内容/【】/：/空白`
  3. `urls = description 里的所有 http(s) URL`
  4. `urls 非空 且 len(text) < 30` → link-only
- 关联文件：`dataset/stories_matched.jsonl`（字段 `tapd_id` 等），用于算优先级。

**优先级**：link-only ∩ stories_matched 的 **19 条**（全部是 alidocs 域名，共 41 个链接）先抓——它们直接影响已关联 merge 的 conformance 评分；其余 266 条后抓。

## 3. 输出物

1. `dataset/story_refs/<tapd_id>.md` — 每个需求一个文件。多个链接按顺序拼接，每段以 `## <url>` 开头。正文去 HTML/导航噪声，保留需求文字；单文件截断到 50k 字符。
2. `dataset/story_refs_index.jsonl` — 每行：
   ```json
   {"tapd_id": "...", "url": "...", "domain": "...", "fetcher": "tapd_api|curl|webbridge", "status": "ok|login_required|not_found|error", "chars": 1234, "fetched_at": "ISO", "error": "可选"}
   ```
3. 断点续跑：重跑时跳过索引里 `status=ok` 的 (tapd_id, url) 对；非 ok 的可重试。

## 4. 域名路由表

按 2026-08-05 统计（链接数）：alidocs.dingtalk.com 442、confluence.yinshantech.cn 50、confluence.adapundi.com 37、www.tapd.cn 30、app.clickup.com 16、axshare 20（c8689m/l35cec）、file.tapd.cn 5、admin.* 8、其他零头。

| 域名 | fetcher | 说明 |
|---|---|---|
| `www.tapd.cn`（需求/wiki 链接） | `tapd_api` | 从 URL 提取需求 id，走 hccli 拉正文（见 §5.1） |
| `file.tapd.cn`（附件） | `tapd_api` | TAPD API 下载附件，doc/docx/pdf 转文本；转不了的标 `error` 并注明格式 |
| `*.axshare.com` | `curl` | **实测死链**：全部重定向到 Axure Cloud access-code 登录墙（`access_code_wall`），公开可访问假设不成立；curl 抓回的是登录墙页（见 §5.2） |
| `alidocs.dingtalk.com` | `webbridge` | 钉钉文档，需用户登录态，走本机 webbridge 守护进程借浏览器会话（见 §5.3） |
| `confluence.yinshantech.cn` / `confluence.adapundi.com` | `webbridge` | 内网 Confluence，需 VPN+登录态，同走 webbridge |
| 其他（clickup、admin.* 等） | 标记 `login_required`，不抓 | 零头，最后人工处理 |

## 5. 各 fetcher 实现要点

### 5.1 tapd_api（全自动）

```bash
PYTHONIOENCODING=utf-8 python "D:/agent-assets/skills/ys-cli/scripts/hccli.py" tapd get-stories --workspace-id 44381896 --params '{"id":"<需求id>"}'
```
- 从 `www.tapd.cn` URL 里提取 id（注意短 id 补前缀 `114438189600`）。
- 取返回的 name + description 作为正文。若被链接需求本身也是 link-only，标 `ok` 但 `error` 注明 `nested_link_only`，不递归。
- file.tapd.cn 附件：用 hccli 的附件下载能力（没有现成子命令就查 hccli `--help`；实在没有就标 `error: no_attachment_api`）。

### 5.2 curl（全自动）

- `curl -sL --max-time 30 <url>`，HTML → 文本（python `html.parser` 或正则去标签均可），正文 <100 字符标 `error: empty_content`。
- **axshare 实测（2026-08-05）**：`*.axshare.com` 链接已全部 301 重定向到 `*.axure.cloud`（Axure Cloud），需要 access code 登录墙，curl 只能抓到登录墙页。正文含 `access code`/`axure cloud` 时标 `login_required`（`access_code_wall`），**不再按「公开可访问」处理**；这 20 个链接（c8689m/l35cec 两套原型）视为无正文参照物。

### 5.3 webbridge（借用户浏览器会话，半自动）

本机 webbridge 守护进程 HTTP API：`http://127.0.0.1:10086/command`。

- 前置检查：先 POST 一个简单 evaluate 确认守护进程和浏览器扩展在线；
  `connection refused` 则运行 `"$USERPROFILE/.kimi-webbridge/bin/kimi-webbridge.exe" start` 拉起后重试；扩展未连上就在报告里写明、该域名全部标 `login_required`，**不要卡住等**。
- 请求格式（Windows Git Bash 下必须写临时 JSON 文件 + `curl.exe --data-binary`，直接内联 JSON 会被转义坑）：
  ```bash
  cat > wb_tmp.json << 'EOF'
  {"action":"navigate","args":{"url":"<目标url>","newTab":false},"session":"ref-fetch"}
  EOF
  curl.exe -s -X POST http://127.0.0.1:10086/command -H "Content-Type: application/json" --data-binary "@wb_tmp.json"
  ```
  navigate 后 sleep 3-5 秒（SPA 渲染），再：
  ```json
  {"action":"evaluate","args":{"code":"document.body.innerText.slice(0,50000)"},"session":"ref-fetch"}
  ```
- 登录墙识别：正文含「登录/扫码/login/sign in」且 <500 字符 → `login_required`。
- **限速**：每次 navigate 间隔 ≥3 秒，同一域名连续 3 次失败就停该域名、标剩余为 `error: aborted_batch`，避免触发风控或把用户浏览器搞卡。
- 用完 `rm -f wb_tmp.json`。

## 6. 集成（抓完后）

- `linker.py` / `judges.py` 构造参照物时，若 description 判定为 link-only 且 `story_refs/<tapd_id>.md` 存在且非空，用 story_refs 内容替代 description（参照物优先级：C 源 spec > C 源 PRD > **story_refs** > B 源 TAPD description）。
- 不要改 `tapd_stories.jsonl` 原文件，参照物富化只走 story_refs 目录。

## 7. 验收（自包含可验证）

依次执行并核对：

1. `python` 重算 link-only 数 = 索引覆盖的 tapd_id 数（应 = 285，含无法抓取的标记行）。
2. 索引统计：`status=ok` 的链接数中，tapd_api + curl 两条自动路线应 ≈ 55（www.tapd.cn 30 + file.tapd.cn 5 + axshare 20，允许个别 not_found）；webbridge 路线的 ok 数取决于用户浏览器会话，如实报告。
3. **优先批 19 条必须全部有索引行**；其中 ok 的，打开任一 `story_refs/<tapd_id>.md` 人工抽查 3 个文件：包含真实需求文字（不是登录页、不是模板占位），每个 >500 字符。
4. 集成后抽 1 条已知 link-only 且已关联的 merge，重跑 `eval score --force` 单条，确认 judge 输入里出现了 story_refs 正文（日志或调试输出可见），且不再报「无法建立映射」类 findings。
5. 全部输出：索引统计表 + 各 fetcher 成功/失败分布 + 抽查结果，回复里给出。

## 8. 约束

- 只许新增/修改 `packages/eval/` 下的代码和 `dataset/story_refs*`，其他模块不许碰。
- 抓取内容只落本地，不外发、不推钉钉。
- webbridge 操作用户真实浏览器，遵守 §5.3 限速与熔断；用户浏览器里正在用的标签页不许关。
