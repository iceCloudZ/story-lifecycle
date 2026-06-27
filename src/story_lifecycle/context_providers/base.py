"""Story context provider protocol — pluggable historical-context injection.

Story-lifecycle is a generic, open-source tool and must NOT hard-depend on any
specific transcript miner. Concrete providers (e.g. the transcript-miner
adapter) are loaded dynamically via config (see ``__init__.py``). This ABC
documents the contract; external providers may also use duck typing.
"""

from abc import ABC, abstractmethod


class BaseStoryContextProvider(ABC):
    """Inject historical context for a story/stage into prompts.

    Implementations return a short (target <500 char) markdown snippet
    summarizing prior work on this story, or ``None`` when no relevant
    history exists. Returning ``None`` causes the prompt section to be
    omitted entirely.
    """

    @abstractmethod
    def get_context(self, story_key: str, workspace: str, stage: str) -> str | None:
        """Return historical-context markdown for this story/stage, or None."""
        ...
