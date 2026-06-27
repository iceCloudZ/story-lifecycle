"""Artifact extractor — shared patch extraction for advance gate and exporter.

Single source of truth for "does this workspace have a model patch?"
Both advance_node's finalize gate and export_predictions() must use this
module so they agree on what counts as a valid patch.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..orchestrator.paths import stage_done_file

log = logging.getLogger("story-lifecycle.artifacts")


@dataclass
class PatchExtractionResult:
    patch: str
    source: str  # "done_json" | "final_patch" | "git_diff" | "git_diff_base" | ""
    reason: str = ""


def extract_model_patch(
    workspace: str | Path,
    story_key: str,
    context: dict | None = None,
) -> PatchExtractionResult:
    """Extract model patch from workspace using priority chain.

    Priority:
    1. .story/done/{story_key}/finalize.json → model_patch field
    2. workspace/final.patch file
    3. git diff (unstaged changes)
    4. git diff {base_commit} (committed changes)
    5. empty
    """
    ws = Path(workspace)
    context = context or {}

    # 1. Done file model_patch
    done_file = stage_done_file(ws, story_key, "finalize")
    if done_file.exists():
        try:
            data = json.loads(done_file.read_text(encoding="utf-8"))
            patch = data.get("model_patch", "")
            if patch:
                return PatchExtractionResult(patch=patch, source="done_json")
        except (json.JSONDecodeError, KeyError):
            pass

    # 2. final.patch file
    final_patch = ws / "final.patch"
    if final_patch.exists():
        content = final_patch.read_text(encoding="utf-8")
        if content.strip():
            return PatchExtractionResult(patch=content, source="final_patch")

    # 3. git diff (unstaged)
    if (ws / ".git").exists():
        patch = _git_diff(ws)
        if patch:
            return PatchExtractionResult(patch=patch, source="git_diff")

        # 4. git diff base_commit (agent may have committed)
        base = context.get("base_commit")
        if base:
            patch = _git_diff(ws, base)
            if patch:
                return PatchExtractionResult(patch=patch, source="git_diff_base")

    return PatchExtractionResult(patch="", source="", reason="no patch found")


def _git_diff(workspace: Path, ref: str | None = None) -> str:
    """Run git diff, return stdout or empty string."""
    args = ["git", "diff"]
    if ref:
        args.append(ref)
    try:
        r = subprocess.run(
            args,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except Exception:
        pass
    return ""
