"""Deterministic pass/fail judgement over story artifacts.

This is the **pure Decider** layer (AGENTS.md: "Decider code must be pure"). The
scenario driver (``testing.web.scenario``) only *drives* the story through the
real browser + HTTP surface; whether the run passes is decided here, exclusively
by checking backend artifacts. No LLM, no DOM inspection — so judgement cannot
be flaky due to AI nondeterminism.

Generic structure
-----------------
:class:`ScenarioJudge` is repo-agnostic: it asserts
  * the drive didn't error,
  * each stage produced its done-file,
  * optionally, named implementation files exist & are non-empty (red→green check),
  * the retrospect.md exists,
  * the configured :class:`TestRunner` passes (pytest / mvn / …),
  * miner linked the transcript back to the story.

Two ready-made subclasses:
  * :class:`CalculatorJudge` — PytestRunner + calculator.py, matches the
    in-process ``test_calculator_real_e2e`` so the two channels stay comparable.
  * :class:`HcOrderJudge` — MavenTestRunner(-pl hc-order-business) +
    WebBridgeDemoUtil.java, the first real-Java-repo scenario.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from testing import asserters
from testing.web.api_client import StoryApiClient
from testing.web.runner import MavenTestRunner, PytestRunner, TestRunner, TestRunnerError
from testing.web.scenario import ScenarioResult

# Default miner DB location (matches tests/e2e/test_calculator_real_e2e.py:15).
_DEFAULT_MINER_DB = (
    Path(__file__).resolve().parents[4] / "packages" / "story-miner" / "data" / "transcripts.db"
)


class Judge:
    """Base class: subclass and override :meth:`judge` for a scenario.

    The base implementation only checks the scenario didn't error out; real
    scenarios add artifact assertions.
    """

    def judge(
        self,
        result: ScenarioResult,
        *,
        workspace: str | Path,
        story_key: str,
        stages: list[str],
        api: StoryApiClient | None = None,
    ) -> None:
        """Raise AssertionError on failure. Return None on success."""
        if result.error:
            raise AssertionError(
                f"scenario drive errored before judgement: {result.error}"
            )


class ScenarioJudge(Judge):
    """Repo-agnostic judge: done-files + optional impl files + test runner + miner.

    Parameters
    ----------
    test_runner:
        How pass/fail is decided at the code level (``MavenTestRunner``,
        ``PytestRunner``, or any :class:`TestRunner`). This is the heart of
        "judgement stays in code, not in AI".
    expected_impl_files:
        Relative paths under ``workspace`` that must exist & be non-empty after
        the run (the red→green proof). Empty = skip this check.
    miner_db:
        Path to transcripts.db for the miner linkage assertion.
    skip_miner:
        True to skip the miner loopback+linkage (real external repos may not be
        wired into the miner config; default True for the generic case).
    """

    def __init__(
        self,
        *,
        test_runner: TestRunner,
        expected_impl_files: list[str] | None = None,
        miner_db: str | Path = _DEFAULT_MINER_DB,
        skip_miner: bool = False,
    ):
        self.test_runner = test_runner
        self.expected_impl_files = list(expected_impl_files or [])
        self.miner_db = Path(miner_db)
        self.skip_miner = skip_miner

    def judge(
        self,
        result: ScenarioResult,
        *,
        workspace: str | Path,
        story_key: str,
        stages: list[str],
        api: StoryApiClient | None = None,
    ) -> None:
        super().judge(
            result, workspace=workspace, story_key=story_key, stages=stages, api=api
        )
        ws_path = Path(workspace)
        run_result = result.to_run_result(stages)

        # 1) Each stage produced its done-file (design/implement/verify ran).
        for stage in stages:
            asserters._stage_done(run_result, stage)  # noqa: SLF001 — shared helper

        # 2) Red→green: named implementation files exist & non-empty.
        for rel in self.expected_impl_files:
            f = ws_path / rel
            assert f.exists(), f"expected implementation file missing: {f}"
            assert f.stat().st_size > 0, f"implementation file empty: {f}"

        # 3) Retrospect artifact.
        asserters.assert_done_retrospect(str(ws_path), story_key)

        # 4) The real decision: tests pass (pytest / mvn / ...).
        try:
            self.test_runner.assert_pass(ws_path)
        except TestRunnerError as exc:
            raise AssertionError(f"test runner '{self.test_runner.name}' failed:\n{exc}") from exc

        # 5) Miner linkage (optional — external repos may not be wired in).
        if not self.skip_miner:
            asserters.run_miner_loopback(str(ws_path))
            asserters.assert_miner_linked(str(self.miner_db), story_key)


class CalculatorJudge(ScenarioJudge):
    """Calculator red→green over the web surface (PytestRunner + calculator.py).

    Mirrors ``testing.asserters`` so the in-process and web channels agree on
    what a "good" run looks like.
    """

    def __init__(self, miner_db: str | Path = _DEFAULT_MINER_DB):
        super().__init__(
            test_runner=PytestRunner(test_path="tests"),
            expected_impl_files=["calculator.py"],
            miner_db=miner_db,
            skip_miner=False,
        )


class HcAllJavaJudge(ScenarioJudge):
    """hc-all (real Java workspace) scenario: MavenTestRunner on a subproject.

    The workspace is the hc-all aggregator (``D:\\hc-all``) — a container of
    independent git repos, not itself a git repo. The AI operates across it;
    the judge runs the Maven test of one subproject module.

    Parameters
    ----------
    subproject:
        Subproject dir under hc-all (e.g. ``hc-config``, ``hc-order``). The impl
        file path and test module are derived from this + ``module``.
    module:
        Maven module to test (e.g. ``hc-config-business``). Default
        ``<subproject>-business``.
    impl_rel_pkg:
        Java package path of the impl file relative to the module's main java
        root, e.g. ``com/ys/hc/config/utils``. Used to locate the expected impl.
    class_name:
        Java class the AI must produce (default ``WebBridgeDemoUtil``).

    skip_miner=True: hc-all is an external workspace not wired into the monorepo
    miner config; miner linkage is a story-lifecycle-internal concern, not part
    of "did the AI correctly implement this Java feature".
    """

    def __init__(
        self,
        *,
        subproject: str = "hc-config",
        module: str | None = None,
        impl_rel_package: str = "com/ys/hc/config/utils",
        class_name: str = "WebBridgeDemoUtil",
        miner_db: str | Path = _DEFAULT_MINER_DB,
    ):
        module = module or f"{subproject}-business"
        impl_rel = f"{subproject}/{module}/src/main/java/{impl_rel_package}/{class_name}.java"
        super().__init__(
            test_runner=MavenTestRunner(
                module=module,
                extra_args=[f"-Dtest={class_name}Test"],
            ),
            expected_impl_files=[impl_rel],
            miner_db=miner_db,
            skip_miner=True,
        )
        self.subproject = subproject
        self.class_name = class_name

    def judge(
        self,
        result: ScenarioResult,
        *,
        workspace: str | Path,
        story_key: str,
        stages: list[str],
        api: StoryApiClient | None = None,
    ) -> None:
        # workspace 是 hc-all 容器；mvnw + pom 在 <workspace>/<subproject>。
        # 动态设 maven_root 后再走通用 ScenarioJudge.judge。
        self.test_runner.maven_root = str(Path(workspace) / self.subproject)  # type: ignore[attr-defined]
        super().judge(
            result, workspace=workspace, story_key=story_key, stages=stages, api=api
        )


class ConsultJudge(ScenarioJudge):
    """Consult E2E judge: greeter red→green + consult 链路落了事件 + (若 spawn)
    advisory 文件可解析 + reviewer adapter ≠ caller + advisory 非空。

    Pure Decider (AGENTS.md): 只读后端 artifacts / DB 事件,不调 LLM,不看 DOM。

    断言点(对齐任务「真实验收」契约):
    a. DB 出现 consult_request + consult_response 事件
    b. 若 consult_response 的 spawn_count > 0:.story/consult/<rid>*.json 存在且可解析
    c. spawn 的 reviewer adapter ≠ caller adapter(decorrelation)
    d. advisory 文本非空

    参数
    ----
    caller_adapter: PRD 故事里 caller 的 adapter(本场景是 claude 跑 headless stage)
    miner_db: 同 ScenarioJudge
    """

    def __init__(
        self,
        *,
        caller_adapter: str = "claude",
        miner_db: str | Path = _DEFAULT_MINER_DB,
    ):
        super().__init__(
            test_runner=PytestRunner(test_path="tests"),
            expected_impl_files=["greeter.py"],
            miner_db=miner_db,
            skip_miner=True,  # consult_demo 是隔离场景,不连真实 miner
        )
        self.caller_adapter = caller_adapter

    def _fetch_consult_events(self, story_key: str) -> list[dict]:
        """从当前进程的 story DB 拉 consult_* 事件。

        WebBridge 场景下 STORY_HOME 被 webbridge_server fixture 设到了 tmp,
        本函数从同一个 STORY_HOME 读 db。失败时返空列表(让断言给出明确报错)。
        """
        import json
        import os
        import sqlite3
        from pathlib import Path

        story_home = os.environ.get("STORY_HOME")
        if not story_home:
            return []
        db_path = Path(story_home) / "story.db"
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT event_type, payload FROM event_log "
                    "WHERE story_key = ? AND event_type IN (?, ?) ORDER BY id",
                    (story_key, "consult_request", "consult_response"),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            out.append({"event_type": r["event_type"], "payload": payload})
        return out

    def judge(
        self,
        result: ScenarioResult,
        *,
        workspace: str | Path,
        story_key: str,
        stages: list[str],
        api: StoryApiClient | None = None,
    ) -> None:
        # 先跑通用 red→green + done-file 断言(greeter.py 写出来 + 测试过)
        super().judge(
            result, workspace=workspace, story_key=story_key, stages=stages, api=api
        )

        ws = Path(workspace)
        events = self._fetch_consult_events(story_key)
        types = [e["event_type"] for e in events]

        # (a) DB 出现 consult_request + consult_response 事件
        assert "consult_request" in types, (
            f"DB 未落 consult_request 事件(claude 没调 story consult?)。"
            f"落了的事件类型:{types}"
        )
        assert "consult_response" in types, (
            f"DB 落了 consult_request 但没有 consult_response(链路断了?)。"
            f"事件类型:{types}"
        )

        # (d) advisory 非空
        resp_events = [e for e in events if e["event_type"] == "consult_response"]
        last_resp = resp_events[-1]["payload"]
        advice = last_resp.get("advice", "")
        assert advice and advice.strip(), (
            f"consult_response.advice 为空(advisory 必须非空,DESIGN §5.1)。"
            f"完整 payload:{last_resp}"
        )

        # (b/c) 若 spawn_count > 0:.story/consult/ 文件可解析 + decorrelation
        spawn_count = int(last_resp.get("spawn_count", 0))
        if spawn_count > 0:
            consult_dir = ws / ".story" / "consult"
            assert consult_dir.exists(), (
                f"spawn_count={spawn_count} 但 {consult_dir} 不存在"
            )
            json_files = sorted(consult_dir.glob("*.json"))
            assert json_files, (
                f"spawn_count={spawn_count} 但 {consult_dir} 下无 .json 文件"
            )
            # 至少一个文件 JSON 可解析
            parsed = None
            parse_err = None
            for jf in json_files:
                try:
                    import json as _json

                    parsed = _json.loads(jf.read_text(encoding="utf-8"))
                    break
                except Exception as exc:
                    parse_err = exc
            assert parsed is not None, (
                f"{len(json_files)} 个 .story/consult/*.json 都解析失败"
                f"(最后一个错:{parse_err})"
            )

            # (c) decorrelation:从 spawn_results 旁路验证 —— 但 consult_response
            # 事件去掉了 spawn_results(只留 spawn_count)。所以我们间接断言:
            # caller_adapter 必须不在 .log 文件名后缀里。request_id 后缀格式
            # 是 <rid>_r<n>_<adapter>(见 consult_orchestrator.py)。
            # 找带 caller_adapter 后缀的文件 → 那是 decorrelation violation
            # 不该出现(Handler 层不真 spawn 违规 adapter)。
            caller_suffixed = [
                f.name for f in json_files if f"_{self.caller_adapter}.json" in f.name
            ]
            assert not caller_suffixed, (
                f"发现 caller({self.caller_adapter})同 adapter 的 spawn 文件:"
                f"{caller_suffixed} —— decorrelation 违规(Handler 应该拒绝)"
            )
            # 至少有一个非 caller adapter 的文件 = 真做了 decorrelation
            non_caller = [
                f.name for f in json_files if f"_{self.caller_adapter}.json" not in f.name
            ]
            assert non_caller, (
                f"spawn_count={spawn_count} 但没找到非 caller adapter 的 spawn "
                f"文件(decorrelation 没生效)。所有文件:{[f.name for f in json_files]}"
            )


# Back-compat alias: the original name targeted hc-order specifically.
HcOrderJudge = HcAllJavaJudge
