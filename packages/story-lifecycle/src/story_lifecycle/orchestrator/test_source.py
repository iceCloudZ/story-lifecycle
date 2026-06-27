"""Test Source — abstract interface for discovering and running tests.

Provides a unified abstraction over different test frameworks:
pytest, maven, npm, etc. Used by the validation layer to
discover relevant test candidates and execute them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestCandidate:
    """A discovered test that may be relevant to a story."""

    name: str = ""
    path: str = ""
    framework: str = ""
    command: str = ""
    scope: str = ""  # file_glob, module, class, function
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult:
    """Result of running a set of test candidates."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    output: str = ""
    duration_ms: float = 0.0
    failures: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class TestSource(ABC):
    """Abstract interface for discovering and running tests."""

    @abstractmethod
    def discover_tests(self, workspace: str) -> list[TestCandidate]:
        """Discover test candidates in the workspace."""
        ...

    @abstractmethod
    def run_tests(self, workspace: str, candidates: list[TestCandidate]) -> TestResult:
        """Run the given test candidates and return results."""
        ...


class PytestTestSource(TestSource):
    """Test source for Python pytest projects."""

    def discover_tests(self, workspace: str) -> list[TestCandidate]:
        """Discover pytest test files."""
        from pathlib import Path

        ws = Path(workspace)
        candidates = []

        # Scan for test files
        for test_dir in ["tests", "test"]:
            dir_path = ws / test_dir
            if dir_path.is_dir():
                for f in dir_path.rglob("test_*.py"):
                    candidates.append(
                        TestCandidate(
                            name=f.stem,
                            path=str(f.relative_to(ws)),
                            framework="pytest",
                            command=f"pytest {f.relative_to(ws)}",
                            scope="file",
                        )
                    )

        return candidates

    def run_tests(self, workspace: str, candidates: list[TestCandidate]) -> TestResult:
        """Run pytest tests."""
        import subprocess

        if not candidates:
            return TestResult()

        # Build pytest command
        paths = [c.path for c in candidates]
        cmd = ["pytest", *paths, "--tb=short", "-q"]

        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = proc.stdout + proc.stderr

            # Simple heuristic parse
            result = TestResult(output=output)
            if proc.returncode == 0:
                result.passed = len(candidates)
            else:
                result.failed = 1
                result.failures = [output[-500:] if len(output) > 500 else output]

            return result
        except subprocess.TimeoutExpired:
            return TestResult(errors=1, output="Test execution timed out (120s)")
        except Exception as e:
            return TestResult(errors=1, output=str(e))


class NpmTestSource(TestSource):
    """Test source for npm-based JavaScript/TypeScript projects."""

    def discover_tests(self, workspace: str) -> list[TestCandidate]:
        from pathlib import Path

        ws = Path(workspace)
        candidates = []

        # Scan for test files
        for pattern in ["**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js"]:
            for f in ws.rglob(pattern):
                candidates.append(
                    TestCandidate(
                        name=f.stem,
                        path=str(f.relative_to(ws)),
                        framework="npm",
                        command="npm test",
                        scope="file",
                    )
                )

        return candidates

    def run_tests(self, workspace: str, candidates: list[TestCandidate]) -> TestResult:
        import subprocess

        try:
            proc = subprocess.run(
                ["npm", "test"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = proc.stdout + proc.stderr
            result = TestResult(output=output)
            if proc.returncode == 0:
                result.passed = len(candidates)
            else:
                result.failed = 1
                result.failures = [output[-500:] if len(output) > 500 else output]
            return result
        except subprocess.TimeoutExpired:
            return TestResult(errors=1, output="npm test timed out (120s)")
        except Exception as e:
            return TestResult(errors=1, output=str(e))


def get_test_source(workspace: str) -> TestSource:
    """Return appropriate test source based on workspace detection."""
    from pathlib import Path

    ws = Path(workspace)

    # Detect framework
    if (ws / "pyproject.toml").exists() or (ws / "pytest.ini").exists():
        return PytestTestSource()
    if (ws / "package.json").exists():
        return NpmTestSource()

    # Default to pytest
    return PytestTestSource()
