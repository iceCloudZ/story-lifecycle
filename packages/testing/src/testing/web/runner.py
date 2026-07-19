"""Pluggable test runners for the judge layer.

The existing ``testing.asserters.assert_verify`` hard-codes
``python -m pytest <workspace>/tests`` — perfect for the calculator scenario but
wrong for a real repo whose tests run under Maven, Gradle, Go, Jest, etc.

This module defines a tiny ``TestRunner`` protocol: ``run(workspace)`` returns the
process exit status (0 = pass). The judge layer accepts any ``TestRunner`` so a
scenario can declare how its pass/fail is decided — judgement stays pure Python
(per AGENTS.md "Decider must be pure"), only the *command* varies.

Runners
-------
* ``MavenTestRunner`` — ``mvnw -pl <module> test`` (hc-order: ``-pl hc-order-business``).
  Works with ``mvnw.cmd`` on Windows / ``mvnw`` elsewhere. The module scoping keeps
  the test run fast (one module, not the whole reactor) and matches where the
  spec/test files land.
* ``PytestRunner`` — ``python -m pytest <path>`` (default, mirrors calculator).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("testing.web.runner")


class TestRunnerError(RuntimeError):
    """The test command ran but failed (non-zero exit). Carries the tail of output."""


@dataclass
class TestRunOutcome:
    exit_code: int
    output_tail: str


class TestRunner:
    """Base protocol. Subclasses implement :meth:`run`."""

    name: str = "base"

    def run(self, workspace: str | Path) -> TestRunOutcome:
        raise NotImplementedError

    def assert_pass(self, workspace: str | Path) -> None:
        """Run and raise :class:`TestRunnerError` if exit != 0 (output tail in message)."""
        outcome = self.run(workspace)
        if outcome.exit_code != 0:
            raise TestRunnerError(
                f"{self.name} failed (exit {outcome.exit_code}):\n{outcome.output_tail}"
            )


def _tail(text: str, n: int = 1200) -> str:
    if not text:
        return ""
    return text[-n:]


class MavenTestRunner(TestRunner):
    """Run ``mvnw test`` in a Maven module.

    Parameters
    ----------
    module:
        Reactor module to test (e.g. ``hc-order-business``); passed via ``-pl`` so
        only that module builds+tests — faster and scoped to where spec files go.
    extra_args:
        Extra args appended to the mvnw command (e.g. ``["-Dtest=WebBridgeDemoUtilTest"]``).
    timeout:
        Subprocess timeout in seconds (Maven can be slow on first run).
    """

    name = "mvn"

    def __init__(
        self,
        *,
        module: str | None = None,
        extra_args: list[str] | None = None,
        timeout: int = 600,
        maven_root: str | Path | None = None,
    ):
        self.module = module
        self.extra_args = list(extra_args or [])
        self.timeout = timeout
        # Where to run mvnw (cwd). Defaults to ``workspace``; for a workspace that
        # is an *aggregator container* (e.g. hc-all) the mvnw + pom live in a
        # subproject, so pass maven_root=<workspace>/<subproject>.
        self.maven_root = maven_root

    def run(self, workspace: str | Path) -> TestRunOutcome:
        ws = Path(workspace)
        root = Path(self.maven_root) if self.maven_root else ws
        # Prefer the repo's own wrapper (version-pinned); fall back to bare mvn.
        if (root / "mvnw.cmd").exists():
            cmd = ["cmd", "/c", "mvnw.cmd"]
        elif (root / "mvnw").exists():
            cmd = ["./mvnw"]
        else:
            cmd = ["mvn"]

        cmd.append("test")
        if self.module:
            cmd += ["-pl", self.module, "-am"]  # -am = also make依赖模块
            # 多模块 Maven 仓库里 business 依赖 api/component，单 -pl 不构建兄弟模块
            # 会导致符号找不到；-am 让 Maven 先把依赖模块装进本次 reactor 一起编译。
            # -am + -Dtest=<X> 的副作用:每个被构建的模块都会用这个测试过滤,
            # 没匹配测试的模块(如 hc-order-api)surefire 会报 "No tests were
            # executed!"。failIfNoTests=false 让那些模块跳过而非失败。
            cmd += ["-DfailIfNoTests=false"]
        cmd += self.extra_args
        cmd += ["-q"]  # quiet: only failures/summary, keeps the tail meaningful

        log.info("MavenTestRunner running in %s: %s", root, " ".join(cmd))
        try:
            r = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TestRunnerError(
                f"{self.name} timed out after {self.timeout}s in {root}"
            ) from exc
        out = (r.stdout or b"").decode("utf-8", "ignore") + (
            r.stderr or b""
        ).decode("utf-8", "ignore")
        return TestRunOutcome(exit_code=r.returncode, output_tail=_tail(out))


class PytestRunner(TestRunner):
    """Run pytest (default, mirrors the calculator scenario's assert_verify)."""

    name = "pytest"

    def __init__(self, test_path: str = "tests", *, timeout: int = 300):
        self.test_path = test_path
        self.timeout = timeout

    def run(self, workspace: str | Path) -> TestRunOutcome:
        ws = Path(workspace)
        cmd = ["python", "-m", "pytest", str(ws / self.test_path), "-q"]
        try:
            r = subprocess.run(
                cmd, cwd=str(ws), capture_output=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise TestRunnerError(
                f"{self.name} timed out after {self.timeout}s in {ws}"
            ) from exc
        out = (r.stdout or b"").decode("utf-8", "ignore")
        return TestRunOutcome(exit_code=r.returncode, output_tail=_tail(out))
