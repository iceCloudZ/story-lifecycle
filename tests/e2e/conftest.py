"""tests/e2e conftest: 默认跳过 real_e2e，除非 -m real_e2e 显式选。

这样 `pytest`（默认）不跑真实 AI 测试（慢/贵/需 key）；
`pytest -m real_e2e` 才跑。
"""
import pytest


def pytest_collection_modifyitems(config, items):
    markexpr = config.getoption("-m") or ""
    if "real_e2e" not in markexpr:
        skip = pytest.mark.skip(
            reason="real_e2e 默认跳过；用 `pytest -m real_e2e` 跑（需 claude/codex CLI + key）"
        )
        for item in items:
            if "real_e2e" in item.keywords:
                item.add_marker(skip)
