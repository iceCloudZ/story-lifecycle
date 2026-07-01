"""Branch name generation — profile-driven, with LLM-translated summary.

Replaces the old hardcoded ``codex/{story_key}-{project}`` rule. The rule now
lives in the profile YAML (``author`` + ``branch_rule``) and supports
``{var}`` placeholders rendered at story-start time.
"""

import logging
import re
from datetime import datetime

log = logging.getLogger("story-lifecycle.branch_naming")

# Placeholders recognized in branch_rule. Unknown ones are left as-is
# (with a warning) so misconfiguration is visible rather than silently empty.
_KNOWN_VARS = {"author", "date", "summary", "story_key", "project"}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def slugify_english(text: str) -> str:
    """Clean free-form text (typically LLM output) into a branch-safe slug.

    Lowercase, spaces/hyphens -> ``_`` (team convention: no hyphens in branch
    names), strip non [a-z0-9_], collapse repeats, trim leading/trailing ``_``,
    cap at ~30 chars on a word boundary.
    """
    s = (text or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)  # spaces / hyphens -> _
    s = re.sub(r"[^a-z0-9_]", "", s)  # drop punctuation / non-ascii
    s = re.sub(r"_{2,}", "_", s)  # collapse runs of _
    s = s.strip("_")
    if len(s) > 30:
        s = s[:30].rstrip("_")
    return s


def render_branch_name(rule: str, variables: dict) -> str:
    """Substitute ``{var}`` placeholders in *rule* using *variables*.

    Unknown placeholders (not in ``_KNOWN_VARS``) are left as-is and logged.
    Missing-but-known vars resolve to empty string (the segment disappears).
    """

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in _KNOWN_VARS:
            log.warning(
                "branch_rule has unknown placeholder {%s}; leaving as-is. Known: %s",
                name,
                sorted(_KNOWN_VARS),
            )
            return match.group(0)
        return str(variables.get(name, ""))

    return _PLACEHOLDER_RE.sub(_replace, rule)


def generate_branch_for_story(
    story_key: str,
    title: str,
    profile_raw: dict,
    project_name: str = "",
) -> str:
    """Build a branch name for one story-project binding.

    Reads ``branch_rule`` + ``author`` from *profile_raw*. The ``{summary}``
    var is an LLM translation of *title*; on any failure it falls back to the
    trailing id of *story_key* so start is never blocked.

    Returns the rendered branch name. If ``branch_rule`` is absent, returns
    empty string (caller should apply its own legacy default).
    """
    rule = profile_raw.get("branch_rule")
    if not rule:
        return ""

    author = profile_raw.get("author", "")
    date = datetime.now().strftime("%m%d")
    summary = _translate_summary(title, story_key)

    variables = {
        "author": author,
        "date": date,
        "summary": summary,
        "story_key": story_key,
        "project": project_name,
    }
    branch = render_branch_name(rule, variables)
    # Tidy: collapse any segment that became empty (e.g. author unset)
    branch = re.sub(r"//+", "/", branch).strip("/")
    log.info(
        "branch for %s -> %s (summary=%r, author=%r, date=%s)",
        story_key,
        branch,
        summary,
        author,
        date,
    )
    return branch


def _translate_summary(title: str, story_key: str) -> str:
    """Translate Chinese title to a short English slug via LLM.

    Falls back to the trailing id of *story_key* on any error or empty result.
    """
    title = (title or "").strip()
    if not title:
        return _fallback_summary(story_key)
    try:
        from ...infra.llm_client import get_llm

        llm = get_llm()
        # DeepSeek-v4-pro 是 reasoning 模型，会先输出推理过程。用 ANSWER: 标记
        # 让它把最终 slug 放在末尾，再用正则提取（比 JSON/纯文本约束更稳）。
        prompt = (
            "Translate to English git branch slug: lowercase, underscore-separated, "
            "3-6 words, drop any 【...】 prefix. End your response with the "
            "slug on its own line prefixed by ANSWER:\n\n"
            f"{title}"
        )
        raw = llm.invoke(prompt, temperature=0.1, max_tokens=600)
        m = re.search(r"ANSWER:\s*([a-z0-9][a-z0-9\- ]+)", raw, re.IGNORECASE)
        slug = slugify_english(m.group(1)) if m else slugify_english(raw)
        if not slug:
            log.warning(
                "LLM summary empty for title=%r (raw=%r), using fallback",
                title,
                raw[:200],
            )
            return _fallback_summary(story_key)
        return slug
    except Exception as e:  # noqa: BLE001 — LLM/network, must not block start
        log.warning("LLM summary failed for title=%r: %s; using fallback", title, e)
        return _fallback_summary(story_key)


def _fallback_summary(story_key: str) -> str:
    """Trailing numeric id of story_key, e.g. tapd-…1065458 -> 1065458."""
    m = re.search(r"(\d+)$", story_key or "")
    return m.group(1) if m else (story_key or "task")
