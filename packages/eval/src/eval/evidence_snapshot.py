"""evidence 目录快照优先读取（快照优先、活目录兜底）。

背景：2026-08-10 UI E2E 事故误删 D:/hc-all/story 下部分 evidence 目录（30/31 已
恢复，1067435 永久缺失）。此后参照物读取一律**先查
dataset/snapshot_v2_20260806/evidence/（快照冻结的完整副本），再兜底活目录**——
快照是唯一可信来源，活目录可能随时间变动/再丢失。

使用方: scanall._evidence_reference / v2_rebase.reference_for /
gate_replay._reference_for。
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_EVIDENCE = PACKAGE_ROOT / "dataset" / "snapshot_v2_20260806" / "evidence"


def evidence_dir_for(evidence_dir: str) -> Path | None:
    """返回实际应读取的 evidence 目录：快照优先，活目录兜底。不存在 → None。"""
    if not evidence_dir:
        return None
    name = os.path.basename(evidence_dir.rstrip("\\/"))
    snap = SNAP_EVIDENCE / name
    if snap.is_dir():
        return snap
    p = Path(evidence_dir)
    return p if p.is_dir() else None


def read_evidence_doc(evidence_dir: str, doc_key: str) -> tuple[str, str]:
    """读 evidence 里的文档（spec > PRD 顺序由调用方控制）。返回 (text, type)。

    doc_key: 'spec' | 'prd'。快照优先 + 活目录兜底。
    """
    d = evidence_dir_for(evidence_dir)
    if d is None:
        return "", ""
    cands = ("spec.md", "Spec.md", "design.md") if doc_key == "spec" else ("PRD.md", "prd.md", "Prd.md")
    for cand in cands:
        f = d / cand
        if f.exists() and f.stat().st_size > 0:
            return f.read_text(encoding="utf-8", errors="replace"), doc_key
    return "", ""


def read_evidence_reference(evidence_dir: str) -> tuple[str, str]:
    """完整参照物读取：spec > PRD（快照优先）。返回 (text, type)；无 → ("", "")。"""
    for doc_key in ("spec", "prd"):
        text, t = read_evidence_doc(evidence_dir, doc_key)
        if text:
            return text, t
    return "", ""
