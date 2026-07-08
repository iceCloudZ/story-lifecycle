"""Local conftest for tests/invariants.

Fixtures are inherited from the parent ``tests/conftest.py``; this file adds
the small fixtures originally defined in ``test_gate_hard_fail.py`` so that
re-exported gate invariant tests can run from this directory.
"""

import pytest


@pytest.fixture
def quality_cfg() -> dict:
    return {"enabled": True, "block_on_open_high_findings": True}


@pytest.fixture
def gate_ctx_at_max(tmp_path):
    """Context whose review_round_count_verify already equals max_retries."""
    return {
        "plan_summary": "verify: 全阶段总览",
        "last_verify_summary": "verify stage completed",
    }
