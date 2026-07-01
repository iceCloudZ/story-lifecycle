"""Context Resolver — reads all entities for a story and validates them.

Pure read operations. No writes, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ContextBundle:
    """All context entities for a story at a point in time."""

    story: dict | None = None
    projects: list[dict] = field(default_factory=list)
    story_projects: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    change_items: list[dict] = field(default_factory=list)
    delivery_artifacts: list[dict] = field(default_factory=list)
    runtime_facts: list[dict] = field(default_factory=list)
    profile: dict | None = None
    revision: int = 0


class ContextResolver:
    """Read-only resolver that assembles a ContextBundle from the database."""

    def __init__(self):
        from ...db import models as _db

        self._db = _db

    def resolve(self, story_key: str) -> ContextBundle:
        """Assemble all context entities for a story."""
        db = self._db

        story = db.get_story(story_key)
        if not story:
            raise ValueError(f"story not found: {story_key}")

        revision = story.get("context_revision", 0)

        # Gather all related entities
        story_projects = db.get_story_projects(story_key)
        project_ids = {sp["project_id"] for sp in story_projects}

        projects = []
        runtime_facts = []
        for pid in project_ids:
            proj = db.get_project(pid)
            if proj:
                projects.append(proj)
            facts = db.get_runtime_facts(pid)
            runtime_facts.extend(facts)

        documents = _get_story_documents(db, story_key)
        change_items = _get_story_change_items(db, story_key)
        delivery_artifacts = _get_story_delivery_artifacts(db, story_key)

        # Load profile if available
        profile = None
        profile_name = story.get("profile", "")
        if profile_name:
            try:
                from ..engine.profile_loader import load_profile

                profile = load_profile(profile_name)
            except Exception:
                profile = None

        return ContextBundle(
            story=story,
            projects=projects,
            story_projects=story_projects,
            documents=documents,
            change_items=change_items,
            delivery_artifacts=delivery_artifacts,
            runtime_facts=runtime_facts,
            profile=profile,
            revision=revision,
        )

    def validate(self, bundle: ContextBundle) -> list[str]:
        """Validate a ContextBundle. Returns a list of error strings."""
        errors: list[str] = []

        if not bundle.story:
            errors.append("story is missing")
            return errors

        # Validate profile/stage
        profile = bundle.profile
        if profile:
            current_stage = bundle.story.get("current_stage", "")
            stages = profile.get("stages", [])
            stage_names = [
                s.get("name", "") if isinstance(s, dict) else str(s) for s in stages
            ]
            if current_stage and current_stage not in stage_names:
                errors.append(
                    f"current_stage '{current_stage}' not found in profile stages: {stage_names}"
                )

        # Validate project paths
        for proj in bundle.projects:
            repo_path = proj.get("repo_path", "")
            if repo_path and not Path(repo_path).exists():
                errors.append(f"project path does not exist: {repo_path}")

        # Validate document refs
        for doc in bundle.documents:
            ref = doc.get("ref", "")
            if ref and not ref.startswith("http"):
                if not Path(ref).exists():
                    errors.append(f"document ref not found: {ref}")

        # Validate URL format
        for artifact in bundle.delivery_artifacts:
            url = artifact.get("url", "")
            if url and not (url.startswith("http://") or url.startswith("https://")):
                errors.append(f"invalid delivery artifact URL: {url}")

        # Validate state values
        valid_delivery_states = {
            "not_started",
            "preparing",
            "review_pending",
            "approved",
            "merged",
            "abandoned",
        }
        for artifact in bundle.delivery_artifacts:
            ds = artifact.get("delivery_state", "")
            if ds and ds not in valid_delivery_states:
                errors.append(
                    f"invalid delivery_state '{ds}' in artifact {artifact['id']}"
                )

        return errors


def _get_story_documents(db, story_key: str) -> list[dict]:
    """Get all documents for a story."""
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_document WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_story_change_items(db, story_key: str) -> list[dict]:
    """Get all change items for a story."""
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_change_item WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_story_delivery_artifacts(db, story_key: str) -> list[dict]:
    """Get all delivery artifacts for a story."""
    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_delivery_artifact WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]
