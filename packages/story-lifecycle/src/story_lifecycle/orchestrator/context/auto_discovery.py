"""Auto Discovery — scan worktrees for DDL, Nacos, config changes.

Three-layer architecture:
- Scanner: read-only inspection of worktree files and git diff
- Decider: pure function comparing scan results with current facts
- Handler: apply mutations in a short transaction
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScanResult:
    """Output of a Scanner run for a single project."""

    project_id: int
    branch: str = ""
    head: str = ""
    sql_files: list[str] = field(default_factory=list)
    nacos_refs: list[dict] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    prd_files: list[str] = field(default_factory=list)
    research_files: list[str] = field(default_factory=list)
    design_files: list[str] = field(default_factory=list)
    plan_files: list[str] = field(default_factory=list)
    test_report_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fallback_mode: bool = False  # True if reading from repo_path (no worktree)


@dataclass
class ContextMutation:
    """A set of changes to apply to context entities."""

    new_documents: list[dict] = field(default_factory=list)
    new_change_items: list[dict] = field(default_factory=list)
    updated_facts: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)


class Scanner:
    """Read-only scanner that inspects a worktree (or falls back to repo_path)."""

    def scan(self, story_key: str, sp: dict, project: dict) -> ScanResult:
        """Scan a story_project binding for auto-discoverable facts.

        Args:
            story_key: the story key
            sp: story_project DB row
            project: project DB row
        """
        worktree_path = sp.get("worktree_path", "")
        project_id = project["id"]

        if worktree_path and Path(worktree_path).exists():
            scan_root = worktree_path
        else:
            # 不再 fallback 到 repo_path:宁可不扫,也不扫错分支污染上下文。
            # worktree_path 为 NULL(未准备)或路径不存在(已删除/未创建)都走这里。
            return ScanResult(
                project_id=project_id,
                errors=[
                    f"worktree 未就绪 (worktree_path={worktree_path!r});"
                    f"请先 POST /worktrees/prepare"
                ],
            )

        result = ScanResult(project_id=project_id, fallback_mode=False)

        # Get current branch/HEAD
        try:
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=scan_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if branch_result.returncode == 0:
                result.branch = branch_result.stdout.strip()

            head_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=scan_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if head_result.returncode == 0:
                result.head = head_result.stdout.strip()
        except Exception:
            pass

        # Scan for SQL/migration files
        result.sql_files = _find_files(scan_root, ["*.sql", "**/migration/**/*.sql"])

        # Scan for config files with potential Nacos refs
        result.config_files = _find_files(
            scan_root,
            [
                "*.yml",
                "*.yaml",
                "*.properties",
                "**/application*.yml",
                "**/bootstrap*.yml",
            ],
        )

        # Extract Nacos Data ID references from config files
        for cf in result.config_files:
            try:
                content = Path(cf).read_text(encoding="utf-8", errors="ignore")
                nacos_refs = _extract_nacos_refs(content)
                for ref in nacos_refs:
                    result.nacos_refs.append(
                        {
                            "file": cf,
                            "data_id": ref,
                        }
                    )
            except Exception:
                pass

        # Scan for PRD/design docs
        result.prd_files = _find_files(
            scan_root,
            [
                "**/story/**/PRD.md",
                "**/prd/**/*.md",
                "**/PRD/**/*.md",
                "**/docs/**/prd*.md",
            ],
        )
        result.research_files = _find_files(
            scan_root,
            ["**/story/**/research.md", "**/docs/**/research*.md"],
        )
        result.design_files = _find_files(
            scan_root,
            [
                "**/story/**/spec.md",
                "**/design/**/*.md",
                "**/docs/**/design*.md",
                "**/DESIGN*.md",
            ],
        )
        result.plan_files = _find_files(
            scan_root,
            ["**/story/**/plan.md", "**/docs/**/plan*.md"],
        )
        result.test_report_files = _find_files(
            scan_root,
            ["**/story/**/test-report.md", "**/docs/**/test-report*.md"],
        )

        return result


class Decider:
    """Pure function: compare scan results with current facts, produce mutations."""

    def merge(
        self,
        current_documents: list[dict],
        current_change_items: list[dict],
        scan_result: ScanResult,
    ) -> ContextMutation:
        """Compare existing context with scan results.

        Returns a ContextMutation describing what to add, update, or flag.
        """
        mutation = ContextMutation()

        # Existing document refs for dedup (normalize separators defensively)
        existing_doc_refs = {
            _normalize_path(d.get("ref", "")) for d in current_documents
        }
        existing_change_refs = {
            _normalize_path(c.get("ref", "")) for c in current_change_items
        }

        # New PRD files
        for prd in scan_result.prd_files:
            if prd not in existing_doc_refs:
                mutation.new_documents.append(
                    {
                        "kind": "prd",
                        "ref": prd,
                        "summary": f"Auto-discovered PRD: {Path(prd).name}",
                        "source": "ai",
                        "evidence_ref": f"file scan in {scan_result.project_id}",
                        "verification_state": "unverified",
                    }
                )

        # New research files
        for research in scan_result.research_files:
            if research not in existing_doc_refs:
                mutation.new_documents.append(
                    {
                        "kind": "research",
                        "ref": research,
                        "summary": f"Auto-discovered research: {Path(research).name}",
                        "source": "ai",
                        "evidence_ref": f"file scan in {scan_result.project_id}",
                        "verification_state": "unverified",
                    }
                )

        # New spec/design files
        for design in scan_result.design_files:
            if design not in existing_doc_refs:
                mutation.new_documents.append(
                    {
                        "kind": "spec",
                        "ref": design,
                        "summary": f"Auto-discovered spec: {Path(design).name}",
                        "source": "ai",
                        "evidence_ref": f"file scan in {scan_result.project_id}",
                        "verification_state": "unverified",
                    }
                )

        # New plan files
        for plan in scan_result.plan_files:
            if plan not in existing_doc_refs:
                mutation.new_documents.append(
                    {
                        "kind": "plan",
                        "ref": plan,
                        "summary": f"Auto-discovered plan: {Path(plan).name}",
                        "source": "ai",
                        "evidence_ref": f"file scan in {scan_result.project_id}",
                        "verification_state": "unverified",
                    }
                )

        # New test reports
        for report in scan_result.test_report_files:
            if report not in existing_doc_refs:
                mutation.new_documents.append(
                    {
                        "kind": "test_report",
                        "ref": report,
                        "summary": f"Auto-discovered test report: {Path(report).name}",
                        "source": "ai",
                        "evidence_ref": f"file scan in {scan_result.project_id}",
                        "verification_state": "unverified",
                    }
                )

        # New SQL files → change items
        for sql in scan_result.sql_files:
            if sql not in existing_change_refs:
                mutation.new_change_items.append(
                    {
                        "kind": "ddl",
                        "ref": sql,
                        "summary": f"Auto-discovered SQL: {Path(sql).name}",
                        "lifecycle_state": "detected",
                        "verification_state": "unverified",
                        "source": "ai",
                        "evidence_ref": f"file scan in {scan_result.project_id}",
                    }
                )

        # New Nacos refs → change items
        for nacos in scan_result.nacos_refs:
            data_id = nacos["data_id"]
            file = nacos["file"]
            ref_key = f"nacos:{data_id}"
            if ref_key not in existing_change_refs:
                mutation.new_change_items.append(
                    {
                        "kind": "nacos",
                        "ref": data_id,
                        "summary": f"Auto-discovered Nacos config in {Path(file).name}",
                        "lifecycle_state": "detected",
                        "verification_state": "unverified",
                        "source": "ai",
                        "evidence_ref": f"found in {file}",
                    }
                )

        return mutation


class Handler:
    """Apply ContextMutations in a short transaction, bumping context_revision."""

    def apply(self, story_key: str, mutation: ContextMutation) -> int:
        """Apply mutations and bump context_revision.

        Returns the new context_revision.
        """
        from ...infra.db import models as db

        import json
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with db._db() as conn:
            # Apply new documents
            for doc in mutation.new_documents:
                ref = _normalize_path(doc["ref"])
                conn.execute(
                    """INSERT INTO story_document
                       (story_key, project_id, kind, ref, summary, source,
                        evidence_ref, verification_state, created_at, updated_at)
                       VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        story_key,
                        doc["kind"],
                        ref,
                        doc["summary"],
                        doc["source"],
                        doc["evidence_ref"],
                        doc["verification_state"],
                        now,
                        now,
                    ),
                )

            # Apply new change items
            for ci in mutation.new_change_items:
                ref = _normalize_path(ci["ref"])
                conn.execute(
                    """INSERT INTO story_change_item
                       (story_key, project_id, kind, ref, summary,
                        lifecycle_state, verification_state, source,
                        evidence_ref, created_at, updated_at)
                       VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        story_key,
                        ci["kind"],
                        ref,
                        ci["summary"],
                        ci["lifecycle_state"],
                        ci["verification_state"],
                        ci["source"],
                        ci["evidence_ref"],
                        now,
                        now,
                    ),
                )

            # Bump context_revision manually (in the same transaction)
            conn.execute(
                "UPDATE story SET context_revision = context_revision + 1,"
                " updated_at = ? WHERE story_key = ?",
                (now, story_key),
            )
            row = conn.execute(
                "SELECT context_revision FROM story WHERE story_key = ?",
                (story_key,),
            ).fetchone()
            new_rev = row["context_revision"] if row else 0

            # Log event
            conn.execute(
                "INSERT INTO event_log (story_key, stage, event_type, payload)"
                " VALUES (?, '', 'context_changed', ?)",
                (
                    story_key,
                    json.dumps(
                        {
                            "new_documents": len(mutation.new_documents),
                            "new_change_items": len(mutation.new_change_items),
                            "new_revision": new_rev,
                        }
                    ),
                ),
            )

            return new_rev


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_path(ref: str) -> str:
    """Normalize a filesystem ref to POSIX form for stable comparison."""
    if not ref:
        return ref
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    try:
        return Path(ref).resolve().as_posix()
    except Exception:
        return ref.replace("\\", "/")


def _find_files(root: str, patterns: list[str]) -> list[str]:
    """Find files matching glob patterns under root.

    Returns POSIX-style absolute paths so equivalent files compare equal
    regardless of OS path separators.
    """
    from pathlib import Path

    results: set[str] = set()
    root_path = Path(root)
    for pattern in patterns:
        try:
            for match in root_path.glob(pattern):
                if match.is_file():
                    results.add(match.resolve().as_posix())
        except Exception:
            pass
    return sorted(results)


def _extract_nacos_refs(content: str) -> list[str]:
    """Extract Nacos Data ID references from config file content.

    Looks for patterns like:
    - spring.cloud.nacos.config.data-id: xxx
    - nacos.data-id: xxx
    - dataId: xxx
    """
    refs: list[str] = []
    for line in content.split("\n"):
        line_stripped = line.strip()
        if "data-id" in line_stripped.lower() or "dataid" in line_stripped.lower():
            # Extract the value part
            for sep in (":", "="):
                if sep in line_stripped:
                    value = (
                        line_stripped.split(sep, 1)[-1].strip().strip('"').strip("'")
                    )
                    if value and len(value) < 200:
                        refs.append(value)
                        break
    return refs
