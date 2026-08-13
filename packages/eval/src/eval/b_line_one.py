# -*- coding: utf-8 -*-
"""B 线单条执行器（子进程，独立沙箱）：pipeline_replay.run_one 封装。

- 生成 gold PRD（参照物：evidence 快照 > story_refs > tapd 描述）
- 结果 JSON 打 stdout（主进程捕获）
- 15 分钟看门狗由主进程 subprocess timeout 强杀
"""
import json
import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(r"D:\github\story-lifecycle\packages\eval")
SANDBOX = PACKAGE_ROOT / "sandbox"
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
sys.path.insert(0, r"D:\github\story-lifecycle\packages\story-lifecycle\src")


def ensure_gold_prd(tapd_id: str) -> Path:
    """参照物（需求正文）→ sandbox/gold/tapd-<id>/PRD.md。

    优先级：evidence 快照目录（目录名前缀短/长 id 直接匹配）> idx 关联 > story_refs > tapd 描述。
    """
    import re as _re

    from eval import v2_rebase
    from eval.evidence_snapshot import SNAP_EVIDENCE, read_evidence_reference

    gold_dir = SANDBOX / "gold" / f"tapd-{tapd_id}"
    gold_dir.mkdir(parents=True, exist_ok=True)
    prd = gold_dir / "PRD.md"
    if prd.exists() and prd.stat().st_size > 100:
        return prd
    text = ""
    # 1. evidence 快照目录直接匹配（目录名前缀 = 短 id 或完整 id）
    if SNAP_EVIDENCE.is_dir():
        # 短 id = 去掉固定 12 位前缀（114438189600），无条件取（完整 id 为 19 位，
        # 不能用 len==18 判定——[-7:] 会带前导 0 错配）
        short = tapd_id[12:]
        for d in SNAP_EVIDENCE.iterdir():
            if d.is_dir() and (d.name.startswith(tapd_id) or d.name.startswith(short)):
                t, _ = read_evidence_reference(str(d))
                if t and len(t) >= 100:
                    text = t
                    break
    # 2. idx 关联（stories_matched 的 entity）
    if not text:
        idx, tapd = v2_rebase.load_match_index()
        for val in idx.values():
            ent = val.get("entity") or {}
            if ent.get("tapd_id") == tapd_id:
                t, _ = read_evidence_reference(ent.get("evidence_dir") or "")
                if not t:
                    t, _ = v2_rebase.reference_for(ent, tapd)
                text = t
                break
    # 3. story_refs（网页正文——清洗：URL 行 + 编辑器 UI 噪音词）
    if not text:
        sr = PACKAGE_ROOT / "dataset" / "snapshot_20260805" / "story_refs" / f"{tapd_id}.md"
        if sr.exists():
            noise = {"菜单", "插入", "AI 创作", "微软雅黑", "正文", "Text", "标题1", "H1", "标题2", "H2",
                     "标题3", "H3", "标题4", "H4", "标题5", "H5", "标题6", "H6", "引用", "默认字体",
                     "图片", "表格", "文件", "链接", "查找替换", "文中提及", "评论", "添加图标",
                     "添加封面", "设置文档信息", "分享", "编辑", "豪", "..."}
            lines = []
            for l in sr.read_text(encoding="utf-8", errors="replace").splitlines():
                s = l.strip()
                if not s or len(s) <= 4:
                    continue
                if s.startswith("http") or s.startswith("# http") or s.startswith("## http"):
                    continue
                if s in noise:
                    continue
                lines.append(s)
            text = "\n".join(lines)
    # 4. tapd 描述兜底
    if not text or len(text) < 100:
        idx, tapd = v2_rebase.load_match_index()
        rec = tapd.get(tapd_id) or {}
        desc = _re.sub(r"<[^>]+>", " ", rec.get("description") or "")
        desc = _re.sub(r"\s+", " ", desc).strip()
        text = desc
    if len(text) < 100:
        text = (text or "") + "\n（需求参照物过薄——agent 可能拒写，如实标注）\n"
    prd.write_text(text[:120_000], encoding="utf-8")
    return prd


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--tapd", required=True)
    ap.add_argument("--story-key", default=None)
    args = ap.parse_args()

    from eval import pipeline_replay
    from story_lifecycle.infra.db import models as db

    pipeline_replay._bootstrap_env()
    ensure_gold_prd(args.tapd)
    story_key = args.story_key or f"tapd-{args.tapd}"
    out = {}
    try:
        r = pipeline_replay.run_one(args.tapd, None, out, auto_advance=True)
    except Exception as exc:  # noqa: BLE001
        r = {"tapd": args.tapd, "status": "infra_error", "error": f"{exc.__class__.__name__}: {exc}"}
    # 终态补查：story 是否真完成 vs 各类 stall（eval 侧分类，不算真完成）
    try:
        s = db.get_story(story_key) or {}
        r["story_status"] = s.get("status", "")
        r["story_stage"] = s.get("current_stage", "")
        r["story_lifecycle"] = s.get("lifecycle_state", "")
        done_dir = Path(s.get("workspace", "")) / ".story" / "done" / story_key
        r["done_files"] = sorted(p.name for p in done_dir.glob("*.json")) if done_dir.exists() else []
    except Exception as exc:  # noqa: BLE001
        r["terminal_check_err"] = str(exc)[:100]
    r.setdefault("story_key", story_key)
    print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
