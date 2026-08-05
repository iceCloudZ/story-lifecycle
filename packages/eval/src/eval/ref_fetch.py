"""ref-fetch — 把 TAPD link-only 需求背后的链接正文抓下来落成参照物。

背景: ``tapd_stories.jsonl`` 6309 条需求里 285 条的 description 去 HTML 后只剩链接
+ 模板占位词,judge 无法建立映射。本模块按域名路由抓取链接正文,落成
``dataset/story_refs/<tapd_id>.md``,供 scanall/judges/verify_links 富化参照物。

路由表（2026-08-05 实测,axshare 已全部重定向到 Axure Cloud access-code 登录墙）:

| 域名 | fetcher | 说明 |
|---|---|---|
| ``www.tapd.cn`` | tapd_api | 从 URL 提取需求 id,hccli get-stories 拉正文 |
| ``file.tapd.cn`` | tapd_api | get-attachments 匹配附件;PNG 标 error:image_attachment,无下载 API 标 error:no_attachment_api |
| ``*.axshare.com`` | curl | 抓 HTML 去标签;登录墙标 login_required,正文<100 字符标 error:empty_content |
| ``alidocs.dingtalk.com`` / ``confluence.*`` | webbridge | 借本机浏览器会话,navigate → evaluate innerText |
| 其他 | login_required | clickup/admin.*/lanhuapp/docs.qq 等,零头人工处理 |

断点续跑: ``story_refs_index.jsonl`` 按 (tapd_id, url) 记 status;``status=ok``
重跑跳过,非 ok 可重试。webbridge 限速（navigate 间隔 ≥3s）与熔断
（同域名连续 3 次失败 → 剩余标 ``error: aborted_batch``）遵守
docs/eval-ref-fetch-task.md §5.3。
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("eval.ref_fetch")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = PACKAGE_ROOT / "dataset"
STORY_REFS_DIR = DATASET_DIR / "story_refs"
INDEX_PATH = DATASET_DIR / "story_refs_index.jsonl"

TAPD_STORIES = DATASET_DIR / "tapd_stories.jsonl"
STORIES_MATCHED = DATASET_DIR / "stories_matched.jsonl"

HCCLI = "D:/agent-assets/skills/ys-cli/scripts/hccli.py"
WORKSPACE_ID = "44381896"
LONG_PREFIX = "114438189600"
WB_API = "http://127.0.0.1:10086/command"
WB_SESSION = "ref-fetch"
WB_DAEMON = str(Path.home() / ".kimi-webbridge/bin/kimi-webbridge.exe")

URL_RE = re.compile(r"https?://[^\s<>\"']+")
MAX_FILE_CHARS = 50_000  # 单 story_refs/<tapd_id>.md 截断
NAVIGATE_MIN_INTERVAL = 3.0  # webbridge navigate 最小间隔（秒）
WB_CONSECUTIVE_FAIL_LIMIT = 3  # 同域名连续失败熔断
WB_RENDER_WAIT = 4.0  # navigate 后等待 SPA 渲染
WB_RENDER_POLL = 3  # 轮询次数

# 正文里可安全剔除的导航/框架噪声行（alicloud/confluence 类页面侧边栏）
NAV_NOISE_LINE = re.compile(
    r"^(新建|主页|目录|搜索|首页|我的文档|团队文件|知识库|最近打开|权限和账号管理|/|"
    r"综合模块|营销模块|投放模块|质检模块|催收模块|指标模块|实验模块|搜索|"
    r"企业内公开|登录|注册|扫码登录|Sign in|Log in|Skip to content)$"
)
LOGIN_WALL_RE = re.compile(r"(登录|扫码|login|sign\s*in|access code|密码)")

# 确定性结果（页面/文档本身如此,重试无意义,不计入熔断）:
# empty_content / image_capture / image_attachment / no_attachment_api /
# no_story_id / no_attachment_id / nested_link_only / story not found / not in story attachments
DETERMINISTIC_ERROR_PREFIXES = (
    "empty_content", "image_capture", "image_attachment", "no_attachment_api",
    "no_story_id", "no_attachment_id", "nested_link_only", "story not found",
    "not in story attachments", "access_code_wall",
)


def _is_transient_error(status: str, err: str) -> bool:
    """异常类错误（网络/超时/浏览器异常）→ 参与熔断计数;确定性结果不计。"""
    if status != "error":
        return False
    return not err.startswith(DETERMINISTIC_ERROR_PREFIXES)


def _is_retryable(rec: dict) -> bool:
    """断点续跑:ok 跳过;确定性错误/登录墙/查无默认跳过（--retry 可强制重试）。"""
    status = rec.get("status")
    if status == "ok":
        return False
    if status == "error" and rec.get("error", "").startswith(DETERMINISTIC_ERROR_PREFIXES):
        return False
    if status in ("login_required", "not_found"):
        return False
    return True


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def clean_text(raw: str) -> str:
    """抓取正文清洗:去 HTML/脚本,去导航噪声行,折叠空白。"""
    text = strip_html(raw)
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) <= 24 and NAV_NOISE_LINE.match(ln):
            continue
        lines.append(ln)
    return "\n".join(lines)


def normalize_url(url: str) -> str:
    """URL 归一:剥 HTML 实体尾巴/跟踪参数。"""
    u = html.unescape(url.split("&quot;")[0].split('",')[0])
    u = u.rstrip(".,;，。；)】》]}")
    try:
        parts = urllib.parse.urlsplit(u)
        q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        drop = {"utm_scene", "utm_source", "utm_medium", "utm_campaign", "app_id",
                "tag_name", "from_iteration_id", "queryToken", "left_tree",
                "_t", "u", "page", "sort_name", "order", "useScene", "groupType",
                "conf_id", "categoryId", "model", "obj_type", "content", "workspace_ids"}
        keep = [(k, v) for k, v in q if k not in drop]
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(keep), parts.fragment))
    except ValueError:
        return u


def extract_urls(description: str) -> list[str]:
    """description 里所有 http(s) URL,归一化后按出现顺序去重。"""
    out: list[str] = []
    for m in URL_RE.findall(description or ""):
        u = normalize_url(m)
        if u not in out:
            out.append(u)
    return out


def is_link_only(description: str) -> bool:
    """link-only 判定（与 docs/eval-ref-fetch-task.md §2 口径一致）。

    1. desc = 去 HTML 标签(description)
    2. text = desc 去掉所有 http(s) URL,再去掉 背景/价值/目标/内容/【】/：/空白
    3. urls = description 里的所有 http(s) URL
    4. urls 非空 且 len(text) < 30 → link-only
    """
    desc = strip_html(description)
    urls = URL_RE.findall(description or "")
    text = URL_RE.sub("", desc)
    for w in ("背景", "价值", "目标", "内容"):
        text = text.replace(w, "")
    text = re.sub(r"[【】：:]", "", text)
    text = re.sub(r"\s+", "", text)
    return bool(urls) and len(text) < 30


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", url)
    return (m.group(1) if m else "").lower()


def fetcher_for(url: str) -> tuple[str, str]:
    """返回 (fetcher, domain)。fetcher: tapd_api | curl | webbridge | login_required。"""
    dom = domain_of(url)
    if dom == "www.tapd.cn" or dom == "file.tapd.cn":
        return "tapd_api", dom
    if dom.endswith(".axshare.com") or dom == "axshare.com":
        return "curl", dom
    if dom == "alidocs.dingtalk.com" or dom.startswith("confluence."):
        if re.search(r"/core/api/resources/img/|\.png($|\.)|\.jpg($|\.)|\.jpeg($|\.)|\.gif($|\.)", url):
            return "image", dom
        return "webbridge", dom
    return "login_required", dom


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def load_link_only_stories() -> list[dict]:
    """读 tapd_stories.jsonl,返回 link-only 需求 [{tapd_id, name, urls}]。"""
    out: list[dict] = []
    if not TAPD_STORIES.exists():
        return out
    for line in TAPD_STORIES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        desc = rec.get("description") or ""
        if not is_link_only(desc):
            continue
        urls = extract_urls(desc)
        if not urls:
            continue
        out.append({"tapd_id": rec["tapd_id"], "name": rec.get("name", ""), "urls": urls})
    return out


def load_matched_tapd_ids() -> set[str]:
    if not STORIES_MATCHED.exists():
        return set()
    ids: set[str] = set()
    for line in STORIES_MATCHED.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tid = json.loads(line).get("tapd_id")
            if tid:
                ids.add(tid)
    return ids


def load_index() -> dict[tuple[str, str], dict]:
    """(tapd_id, url) → 索引行。"""
    idx: dict[tuple[str, str], dict] = {}
    if INDEX_PATH.exists():
        for line in INDEX_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            idx[(rec["tapd_id"], rec["url"])] = rec
    return idx


def _append_index_row(rec: dict) -> None:
    with open(INDEX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _dedupe_index() -> int:
    """索引去重重写:每 (tapd_id, url) 保留最后一行（后写覆盖语义）,返回保留行数。"""
    idx = load_index()
    if len(idx) == 0:
        return 0
    # 保持首次出现的相对顺序
    order: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for (k, _rec) in idx.items():
        if k not in seen:
            seen.add(k)
            order.append(k)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        for k in order:
            f.write(json.dumps(idx[k], ensure_ascii=False) + "\n")
    return len(order)


# ---------------------------------------------------------------------------
# tapd_api fetcher
# ---------------------------------------------------------------------------


def _run_hccli(args: list[str], timeout: int = 60) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, HCCLI, "tapd", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"hccli {args[0]} 失败: {r.stderr[:300] or r.stdout[:300]}")
    out = r.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def tapd_story_id_from_url(url: str) -> str | None:
    """www.tapd.cn URL → 需求 id（story/detail | tapdm | dialog_preview_id 三种形态）。"""
    for pat in (
        r"/story/detail/(\d{15,20})",
        r"/prong/stories/view/(\d{15,20})",
        r"/tapdm/\d+/entity/story/(\d{15,20})",
        r"dialog_preview_id=story_(\d{15,20})",
    ):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def fetch_tapd_story(url: str) -> dict:
    """www.tapd.cn 链接:hccli get-stories 拉 name + description 作正文。"""
    tid = tapd_story_id_from_url(url)
    if not tid:
        return {"status": "error", "error": "no_story_id", "text": ""}
    try:
        res = _run_hccli(
            ["get-stories", "--workspace-id", WORKSPACE_ID, "--params", json.dumps({"id": tid})]
        )
    except RuntimeError as e:
        return {"status": "error", "error": f"hccli: {e}", "text": ""}
    data = res.get("data") or {}
    stories: list = []
    if isinstance(data, dict):
        for k in ("stories", "list", "items"):
            if isinstance(data.get(k), list):
                stories = data[k]
                break
    elif isinstance(data, list):
        stories = data
    if not stories:
        return {"status": "not_found", "error": "story not found", "text": ""}
    s = stories[0].get("Story", stories[0]) if isinstance(stories[0], dict) else stories[0]
    name = str(s.get("name") or "")
    desc = str(s.get("description") or "")
    text = clean_text(f"{name}\n{desc}")
    if is_link_only(desc):
        return {"status": "ok", "text": text, "error": "nested_link_only"}
    return {"status": "ok", "text": text, "error": ""}


def fetch_tapd_attachment(url: str, story_tapd_id: str) -> dict:
    """file.tapd.cn 链接:get-attachments 匹配附件;PNG → image_attachment,其余无下载 API。"""
    m = re.search(r"attachments/(?:download|preview_attachments)/(\d{15,20})/", url)
    if not m:
        # tfl/captures/*.png 截图 → 图片无正文
        if ".png" in url or ".jpg" in url or ".jpeg" in url or ".gif" in url:
            return {"status": "error", "error": "image_capture", "text": ""}
        return {"status": "error", "error": "no_attachment_id", "text": ""}
    att_id = m.group(1)
    try:
        res = _run_hccli(
            ["get-attachments", "--workspace-id", WORKSPACE_ID,
             "--params", json.dumps({"story_id": story_tapd_id})]
        )
    except RuntimeError as e:
        return {"status": "error", "error": f"hccli: {e}", "text": ""}
    data = res.get("data") if isinstance(res, dict) else res
    items: list = []
    if isinstance(data, dict):
        for k in ("Attachment", "attachments", "list", "data"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    elif isinstance(data, list):
        items = data
    hit = None
    for it in items:
        a = it.get("Attachment", it) if isinstance(it, dict) else it
        if str(a.get("id") or a.get("attachment_id") or "") == att_id:
            hit = a
            break
    if hit is None:
        return {"status": "error", "error": f"attachment {att_id} not in story attachments", "text": ""}
    fname = str(hit.get("name") or "").lower()
    if fname.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return {"status": "error", "error": "image_attachment", "text": ""}
    return {"status": "error", "error": "no_attachment_api", "text": ""}


# ---------------------------------------------------------------------------
# curl fetcher
# ---------------------------------------------------------------------------


def fetch_curl(url: str) -> dict:
    """curl 抓 HTML 去标签留文字;登录墙 → login_required,<100 字符 → error:empty_content。"""
    try:
        r = subprocess.run(
            ["curl.exe", "-sL", "--max-time", "30", "-A",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", url],
            capture_output=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"status": "error", "error": f"curl: {e}", "text": ""}
    raw = r.stdout.decode("utf-8", "replace")
    text = clean_text(raw)
    if len(text) < 100:
        low = text.lower()
        if "access code" in low or "axure cloud" in low or (LOGIN_WALL_RE.search(text) and len(text) < 300):
            return {"status": "login_required", "error": "access_code_wall", "text": text}
        return {"status": "error", "error": "empty_content", "text": text}
    if LOGIN_WALL_RE.search(text) and len(text) < 300:
        return {"status": "login_required", "error": "login_wall", "text": text}
    return {"status": "ok", "text": text, "error": ""}


# ---------------------------------------------------------------------------
# webbridge fetcher
# ---------------------------------------------------------------------------


class WebbridgeError(RuntimeError):
    pass


def _wb_call(action: str, args: dict) -> dict:
    body = json.dumps({"action": action, "args": args, "session": WB_SESSION}).encode("utf-8")
    req = urllib.request.Request(WB_API, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except OSError as e:
        raise WebbridgeError(f"webbridge 不可达: {e}") from e


def wb_daemon_alive() -> bool:
    try:
        res = _wb_call("evaluate", {"code": "document.title"})
        return bool(res.get("ok"))
    except WebbridgeError:
        return False


def wb_start_daemon() -> bool:
    """拉起 webbridge 守护进程,最多等 20s。"""
    if not Path(WB_DAEMON).exists():
        return False
    try:
        subprocess.Popen([WB_DAEMON, "start"], creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        return False
    for _ in range(20):
        if wb_daemon_alive():
            return True
        time.sleep(1)
    return False


_last_nav_ts = 0.0

# alidocs 正文在 iframe 里,body 只渲染侧边栏树;iframe 同源可读时优先取 iframe 正文
_WB_EXTRACT_JS = """
(() => {
  const f = document.querySelector('iframe');
  if (f) {
    try {
      const t = f.contentDocument.body.innerText;
      if (t && t.trim().length > 0) return t.slice(0, 60000);
    } catch (e) {}
  }
  return document.body.innerText.slice(0, 60000);
})()
"""


def fetch_webbridge(url: str, domain: str) -> dict:
    """navigate → 等待渲染稳定 → 提取正文（iframe 优先）。"""
    global _last_nav_ts
    if not wb_daemon_alive():
        if not wb_start_daemon():
            return {"status": "login_required", "error": "webbridge_offline", "text": ""}
    # 限速:同会话 navigate 间隔 ≥3s
    wait = NAVIGATE_MIN_INTERVAL - (time.monotonic() - _last_nav_ts)
    if wait > 0:
        time.sleep(wait)
    try:
        nav = _wb_call("navigate", {"url": url, "newTab": False})
        _last_nav_ts = time.monotonic()
    except WebbridgeError as e:
        return {"status": "error", "error": str(e), "text": ""}
    if not nav.get("ok") or not (nav.get("data") or {}).get("success"):
        msg = (nav.get("error") or {}).get("message", "navigate failed")
        return {"status": "error", "error": str(msg)[:200], "text": ""}
    # 轮询:文本稳定（连续两次一致）或超时即取
    text, prev = "", None
    for i in range(WB_RENDER_POLL + 2):
        time.sleep(WB_RENDER_WAIT if i == 0 else 3.0)
        try:
            ev = _wb_call("evaluate", {"code": _WB_EXTRACT_JS})
        except WebbridgeError as e:
            return {"status": "error", "error": str(e), "text": ""}
        if not ev.get("ok"):
            msg = (ev.get("error") or {}).get("message", "evaluate failed")
            return {"status": "error", "error": str(msg)[:200], "text": ""}
        val = (ev.get("data") or {}).get("value") or ""
        if prev is not None and val == prev and i >= 1:
            text = val
            break
        prev = val
        text = val
    text = clean_text(text)
    if len(text) < 300 and LOGIN_WALL_RE.search(text):
        return {"status": "login_required", "error": "login_wall", "text": text}
    if len(text) < 100:
        return {"status": "error", "error": "empty_content", "text": text}
    return {"status": "ok", "text": text, "error": ""}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _fetch_one(fetcher: str, url: str, story: dict) -> dict:
    if fetcher == "tapd_api":
        if domain_of(url) == "file.tapd.cn":
            return fetch_tapd_attachment(url, story["tapd_id"])
        return fetch_tapd_story(url)
    if fetcher == "curl":
        return fetch_curl(url)
    if fetcher == "webbridge":
        return fetch_webbridge(url, domain_of(url))
    if fetcher == "image":
        return {"status": "error", "error": "image_capture", "text": ""}
    return {"status": "login_required", "error": "manual_domain", "text": ""}


def _parse_story_ref(path: Path) -> list[tuple[str, str]]:
    """解析既有 story_refs/<tapd_id>.md → [(url, text)]（断点续跑合并用）。"""
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    cur_url, cur_lines = "", []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            if cur_url:
                out.append((cur_url, "\n".join(cur_lines)))
            cur_url, cur_lines = line[3:].strip(), []
        elif cur_url:
            cur_lines.append(line)
    if cur_url:
        out.append((cur_url, "\n".join(cur_lines)))
    return out


def _write_story_ref(story: dict, sections: list[tuple[str, str, str]]) -> None:
    """按 URL 顺序拼 story_refs/<tapd_id>.md;每段 `## <url>`,单文件 50k 截断。

    断点续跑合并:本次未处理的 url 段落从旧文件保留（新结果覆盖同 url 旧段落）。
    """
    STORY_REFS_DIR.mkdir(parents=True, exist_ok=True)
    path = STORY_REFS_DIR / f"{story['tapd_id']}.md"
    merged: dict[str, str] = dict(_parse_story_ref(path))
    for url, status, text in sections:
        if status == "ok":
            merged[url] = text
        else:
            merged.pop(url, None)
    parts: list[str] = []
    used = 0
    for url, text in merged.items():
        head = f"## {url}\n"
        if used + len(head) + len(text) > MAX_FILE_CHARS:
            remain = MAX_FILE_CHARS - used
            if remain > 200:
                parts.append(head + text[: remain - len(head) - 60] + "\n... [截断]")
            break
        parts.append(head + text + "\n")
        used += len(head) + len(text)
    md = "\n".join(parts)
    if md.strip():
        path.write_text(md, encoding="utf-8")
    elif path.exists():
        path.unlink()  # 全部失败 → 删掉旧文件,避免空文件冒充参照物


def run_fetch(
    priority_only: bool = False,
    tapd_ids: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    routes: list[str] | None = None,
    retry: bool = False,
) -> dict:
    """抓 link-only 需求链接正文,更新 story_refs + 索引。断点续跑:status=ok 跳过。

    ``retry=True`` 时连确定性错误（empty_content/login_wall 等）也强制重试。
    """
    stories = load_link_only_stories()
    if priority_only:
        matched = load_matched_tapd_ids()
        stories = [s for s in stories if s["tapd_id"] in matched]
        log.info("优先批(link-only ∩ stories_matched): %d 条", len(stories))
    if tapd_ids:
        want = set(tapd_ids)
        stories = [s for s in stories if s["tapd_id"] in want]

    idx = load_index()
    todo: list[tuple[dict, str]] = []
    for s in stories:
        for url in s["urls"]:
            rec = idx.get((s["tapd_id"], url))
            if rec and not (retry or _is_retryable(rec)):
                continue
            if routes and fetcher_for(url)[0] not in routes:
                continue
            todo.append((s, url))
    if limit:
        todo = todo[:limit]
    log.info("待抓 (tapd_id, url): %d 个,涉及 %d 条需求", len(todo), len({s['tapd_id'] for s, _ in todo}))

    if dry_run:
        return {"todo": len(todo), "stories": len(stories), "dry_run": True}

    counts: dict[str, int] = {}
    errors: list[str] = []
    wb_fail_domains: dict[str, int] = {}  # 熔断:连续异常计数（login_required 是确定性结果,不计）
    aborted_domains: set[str] = set()
    story_sections: dict[str, list[tuple[str, str, str]]] = {}

    for i, (story, url) in enumerate(todo, 1):
        fetcher, domain = fetcher_for(url)
        if fetcher == "webbridge" and domain in aborted_domains:
            status, err, text = "error", "aborted_batch", ""
        elif fetcher == "webbridge" and wb_fail_domains.get(domain, 0) >= WB_CONSECUTIVE_FAIL_LIMIT:
            status, err, text = "error", "aborted_batch", ""
        else:
            try:
                res = _fetch_one(fetcher, url, story)
                status, err, text = res["status"], res.get("error", ""), res.get("text", "")
            except Exception as e:  # noqa: BLE001
                log.exception("fetch 异常 %s %s", story["tapd_id"], url)
                status, err, text = "error", str(e)[:200], ""
        counts[status] = counts.get(status, 0) + 1
        if status == "ok":
            wb_fail_domains[domain] = 0
        elif fetcher == "webbridge" and _is_transient_error(status, err):
            wb_fail_domains[domain] = wb_fail_domains.get(domain, 0) + 1
            if wb_fail_domains[domain] >= WB_CONSECUTIVE_FAIL_LIMIT:
                aborted_domains.add(domain)
                log.warning("webbridge 域名 %s 连续异常 %d 次,熔断剩余链接", domain, WB_CONSECUTIVE_FAIL_LIMIT)
        if err and err != "aborted_batch":
            errors.append(f"{story['tapd_id']} {url}: {err}")

        story_sections.setdefault(story["tapd_id"], []).append((url, status, text))
        _append_index_row({
            "tapd_id": story["tapd_id"],
            "url": url,
            "domain": domain,
            "fetcher": fetcher,
            "status": status,
            "chars": len(text),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": err or None,
        })
        if i % 25 == 0 or i == len(todo):
            log.info("ref-fetch 进度 %d/%d, ok=%s", i, len(todo), counts)

    # 落 story_refs 文件（每次跑完重写全部相关文件,断点续跑时之前 ok 的段来自索引 text 丢失——
    # 因此重写时需回填索引里的历史 ok 文本;索引不存 text,所以只重写本次处理的文件）
    for tid, sections in story_sections.items():
        _write_story_ref({"tapd_id": tid}, sections)
    _dedupe_index()

    return {
        "stories_total": len({s["tapd_id"] for s, _ in todo}),
        "todo": len(todo),
        "fetched": len(todo),
        "by_status": counts,
        "errors": errors[:20],
        "index": str(INDEX_PATH),
        "story_refs_dir": str(STORY_REFS_DIR),
    }


def index_stats() -> dict:
    """索引统计:按 fetcher/status 分布 + ok 链接数 + 覆盖 tapd_id 数。"""
    idx = load_index()
    by_fetcher: dict[str, dict[str, int]] = {}
    by_status: dict[str, int] = {}
    for rec in idx.values():
        f = rec.get("fetcher", "?")
        s = rec.get("status", "?")
        by_fetcher.setdefault(f, {})
        by_fetcher[f][s] = by_fetcher[f].get(s, 0) + 1
        by_status[s] = by_status.get(s, 0) + 1
    covered = {tid for tid, _ in idx}
    ok_links = sum(1 for rec in idx.values() if rec.get("status") == "ok")
    files = {p.stem for p in STORY_REFS_DIR.glob("*.md")} if STORY_REFS_DIR.exists() else set()
    return {
        "index_rows": len(idx),
        "covered_tapd_ids": len(covered),
        "ok_links": ok_links,
        "by_fetcher": by_fetcher,
        "by_status": by_status,
        "story_refs_files": len(files),
    }


# ---------------------------------------------------------------------------
# 集成:参照物读取（scanall / verify_links / judges 共用）
# ---------------------------------------------------------------------------


def load_story_ref(tapd_id: str) -> str:
    """story_refs/<tapd_id>.md 正文,不存在/为空返回 "".。"""
    p = STORY_REFS_DIR / f"{tapd_id}.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    return text if len(text) >= 100 else ""


def reference_for_tapd(tapd: dict, tapd_id: str) -> tuple[str, str]:
    """构造 TAPD 参照物:link-only 且 story_refs 富化成功 → (story_refs 正文, "story_refs");
    否则 (description 去 HTML 文本, "tapd");查无 → ("", "")。"""
    if not tapd_id:
        return "", ""
    rec = tapd.get(tapd_id) or {}
    desc = rec.get("description") or ""
    if is_link_only(desc):
        ref = load_story_ref(tapd_id)
        if ref:
            return ref, "story_refs"
    text = strip_html(desc).strip()
    return (text, "tapd") if text else ("", "")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, encoding="utf-8")
    print(json.dumps(run_fetch(priority_only="--priority" in sys.argv), ensure_ascii=False))
