"""Regression tests for accidental cross-story domain contamination."""

import importlib.util


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def test_story_lifecycle_does_not_expose_contact_apis(isolated_story_home):
    from story_lifecycle.orchestrator.service.api import app

    def _paths(routes):
        out = []
        for route in routes:
            # FastAPI 新版 include_router 产生 _IncludedRouter 包装,需展开
            if type(route).__name__ == "_IncludedRouter":
                router = getattr(route, "original_router", None)
                if router is not None:
                    out.extend(_paths(getattr(router, "routes", [])))
                continue
            p = getattr(route, "path", None)
            if p:
                out.append(p)
        return out

    contact_routes = [p for p in _paths(app.routes) if p.startswith("/api/contact")]

    assert contact_routes == []


def test_story_lifecycle_does_not_package_contact_modules():
    unexpected_modules = [
        "story_lifecycle.contact_verification",
        "story_lifecycle.validators.contact_reachability",
        "story_lifecycle.validators.email_validator",
        "story_lifecycle.validators.phone_validator",
        "story_lifecycle.validators.combined_validator",
        "story_lifecycle.validators.name_validator",
    ]

    assert [name for name in unexpected_modules if _module_exists(name)] == []
