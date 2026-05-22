"""Tool registry — extensible dispatch for stage execution."""

_TOOLS: dict[str, type] = {}


def register_tool(name: str, cls: type):
    _TOOLS[name] = cls


def get_tool(name: str):
    """Return a tool instance by name. Falls back to stage_tool."""
    _ensure_registered()
    cls = _TOOLS.get(name, _TOOLS.get("stage_tool"))
    return cls()


def available_tools() -> list[str]:
    _ensure_registered()
    return list(_TOOLS.keys())


def _ensure_registered():
    if not _TOOLS:
        from .stage_tool import StageTool
        from .skill_tool import SkillTool
        from .research_tool import ResearchTool
        from .benchmark_tool import BenchmarkTool
        from .review_tool import ReviewTool

        register_tool("stage_tool", StageTool)
        register_tool("skill_tool", SkillTool)
        register_tool("research_tool", ResearchTool)
        register_tool("benchmark_tool", BenchmarkTool)
        register_tool("review_tool", ReviewTool)
