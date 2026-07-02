"""Write run artifacts for init-knowledge.

Saves detection-result.json and scope-decision.yaml into
.story/knowledge/runs/init-knowledge-<timestamp>/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .detector import DetectionResult
from .paths import run_dir
from .scope import ScopeRecommendation


def create_run_id() -> str:
    """Generate a run ID from current timestamp."""
    return f"init-knowledge-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def write_run_artifacts(
    workspace: str | Path,
    run_id: str,
    detection: DetectionResult,
    scope: ScopeRecommendation,
    mode: str = "interactive",
) -> Path:
    """Write all run artifacts and return the run directory path."""
    rd = run_dir(workspace, run_id)
    rd.mkdir(parents=True, exist_ok=True)

    _write_detection_result(rd, detection)
    _write_scope_decision(rd, detection, scope, mode)

    return rd


def _write_detection_result(rd: Path, detection: DetectionResult) -> None:
    """Write detection-result.json."""
    data = {
        "apiVersion": "knowledge/v1",
        "kind": "ProjectDetectionResult",
        "root": detection.root.replace("\\", "/"),
        "product_guess": detection.product_guess,
        "services": [
            {
                "id": s.id,
                "path": s.path.replace("\\", "/"),
                "type": s.type,
                "included": s.included,
                "reason": s.reason,
            }
            for s in detection.services
        ],
        "frontends": [
            {
                "id": f.id,
                "path": f.path.replace("\\", "/"),
                "type": f.type,
                "included": f.included,
                "reason": f.reason,
            }
            for f in detection.frontends
        ],
        "doc_dirs": detection.doc_dirs,
        "spec_dirs": detection.spec_dirs,
        "bug_dirs": detection.bug_dirs,
        "test_dirs": detection.test_dirs,
        "ignored_or_generated": detection.ignored_or_generated,
        "existing_knowledge": detection.existing_knowledge,
        "codegraph_cache": detection.codegraph_cache,
        "warnings": detection.warnings,
        "file_stats": detection.file_stats,
    }
    (rd / "detection-result.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_scope_decision(
    rd: Path,
    detection: DetectionResult,
    scope: ScopeRecommendation,
    mode: str,
) -> None:
    """Write scope-decision.yaml."""
    lines = [
        "apiVersion: knowledge/v1",
        "kind: InitKnowledgeScopeDecision",
        f"product: {detection.product_guess}",
        f"mode: {mode}",
        "",
        "include:",
    ]
    for svc in scope.included:
        lines.append(f"  - {svc.id}")
    lines.append("")
    lines.append("exclude:")
    for svc in scope.excluded:
        lines.append(f"  - {svc.id}")
    lines.append("")
    lines.extend(
        [
            "provider:",
            "  codegraph: optional",
            "fallback:",
            "  - rg",
            "  - filesystem",
        ]
    )
    (rd / "scope-decision.yaml").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
