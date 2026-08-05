"""gold 适配器（round2 §3.1）— 快照 TAPD + story_refs → 回放 PRD + 历史 diff 组装。

从 snapshot_20260805 只读取数：
- TAPD name + description（link-only 时用 story_refs 正文替代/拼接）→ PRD.md
- 该 story 历史交付的 merge diff（git diff <merge>^1 <merge>，多 merge 拼接）→ delivery.diff
- 产出沙箱目录 sandbox/gold/<story_key>/ 下
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP = PACKAGE_ROOT / "dataset" / "snapshot_20260805"
GOLD_DIR = PACKAGE_ROOT / "sandbox" / "gold"

HC_ALL = Path("D:/hc-all")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_link_only(desc: str) -> bool:
    urls = re.findall(r"https?://[^\s<>\"']+", desc or "")
    text = re.sub(r"https?://[^\s<>\"']+", "", _strip_html(desc))
    for w in ("背景", "价值", "目标", "内容"):
        text = text.replace(w, "")
    text = re.sub(r"[【】：:]", "", text)
    text = re.sub(r"\s+", "", text)
    return bool(urls) and len(text) < 30


def _load_tapd() -> dict[str, dict]:
    out = {}
    for l in (SNAP / "tapd_stories.jsonl").read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            out[r["tapd_id"]] = r
    return out


def _load_matched() -> dict[str, dict]:
    out = {}
    for l in (SNAP / "stories_matched.jsonl").read_text(encoding="utf-8").splitlines():
        if l.strip():
            e = json.loads(l)
            if e.get("tapd_id"):
                out[e["tapd_id"]] = e
    return out


def _load_scores() -> list[dict]:
    return [
        json.loads(l)
        for l in (SNAP / "merge_scores.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def _load_deliveries() -> dict[tuple[str, str], dict]:
    out = {}
    for l in (SNAP / "deliveries.jsonl").read_text(encoding="utf-8").splitlines():
        if l.strip():
            d = json.loads(l)
            out[(d["repo"], d["merge_hash"])] = d
    return out


def _repo_path(repo: str) -> Path | None:
    p = HC_ALL / "frontends" / "hc-admin" if repo == "hc-admin" else HC_ALL / repo
    return p if p.is_dir() else None


def diff_for(repo: str, merge_hash: str, max_chars: int = 80_000) -> str:
    rp = _repo_path(repo)
    if rp is None:
        return ""
    base = f"{merge_hash}^1"
    r = subprocess.run(
        ["git", "--no-pager", "diff", "-U2", base, merge_hash],
        cwd=str(rp), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    if r.returncode != 0:
        return ""
    return r.stdout[:max_chars]


def build_gold(tapd_id: str, story_key: str) -> dict:
    """生成沙箱 gold：PRD.md + delivery.diff，返回元信息。"""
    tapd = _load_tapd()
    matched = _load_matched()
    rec = tapd.get(tapd_id) or {}
    ent = matched.get(tapd_id) or {}

    # PRD 正文：name + description；name 空时用 entity name 兜底
    name = rec.get("name") or ent.get("name") or ""
    desc = rec.get("description") or ""
    parts = [f"# {name or tapd_id}", ""]
    refs_text = ""
    if _is_link_only(desc):
        ref_path = SNAP / "story_refs" / f"{tapd_id}.md"
        if ref_path.exists():
            refs_text = ref_path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"> 需求链接正文（story_refs 富化）：\n\n{refs_text[:60_000]}")
    else:
        parts.append(_strip_html(desc))
    prd = "\n".join(parts)

    # 历史 diff：该 story 所有 merge 拼接
    scores = _load_scores()
    deliveries = _load_deliveries()
    merge_list = []
    diff_parts = []
    for r in scores:
        if r.get("tapd_id") == tapd_id and r.get("conformance_score"):
            dl = deliveries.get((r["repo"], r["merge_hash"]))
            if dl is None:
                continue
            d = diff_for(r["repo"], r["merge_hash"])
            if d:
                diff_parts.append(f"===== {r['repo']}:{r['merge_hash'][:10]} =====\n{d}")
                merge_list.append(f"{r['repo']}:{r['merge_hash'][:10]}")

    out_dir = GOLD_DIR / (story_key or tapd_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    prd_path = out_dir / "PRD.md"
    prd_path.write_text(prd, encoding="utf-8")
    diff_path = out_dir / "delivery.diff"
    diff_path.write_text("\n\n".join(diff_parts) or "（无历史 diff）", encoding="utf-8")

    return {
        "tapd_id": tapd_id,
        "story_key": story_key,
        "name": name[:80],
        "prd_path": str(prd_path),
        "prd_chars": len(prd),
        "diff_path": str(diff_path),
        "diff_chars": sum(len(x) for x in diff_parts),
        "merges": merge_list,
        "link_only": _is_link_only(desc),
        "story_refs_used": bool(refs_text),
    }


if __name__ == "__main__":
    import sys

    tid, sk = sys.argv[1], sys.argv[2]
    print(json.dumps(build_gold(tid, sk), ensure_ascii=False, indent=2))
