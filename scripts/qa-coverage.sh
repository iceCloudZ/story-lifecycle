#!/usr/bin/env bash
# QA coverage runner — story-lifecycle baseline.
# Usage: bash scripts/qa-coverage.sh
# Produces terminal + HTML + XML reports under .story-runs/coverage-*

set -euo pipefail

PYTHON="./.venv-monorepo-test/Scripts/python.exe"

$PYTHON -m pytest packages/story-lifecycle/tests/ \
  --cov=packages/story-lifecycle/src/story_lifecycle \
  --cov-report=term-missing \
  --cov-report=html \
  --cov-report=xml
