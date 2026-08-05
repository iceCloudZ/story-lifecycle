"""回归守卫（设计 14 §2.3）— 防已删除的调度机制被偷偷加回。

设计 13 删掉了三套旧调度机制（driver poll loop / orphan 认领 / done-file
watcher），归一为全局编排线程（OrchestratorThread）。本文件用**静态扫描**
锁定它们不回潮——不依赖运行时，纯 ast / 源码文本检查。

守卫内容：
1. 五个旧符号不得以「函数/方法定义」形式重新出现（注释里的历史引用不算）。
2. ``planner.continue_orchestrator_agent`` 函数体（不含 docstring）≤ 30 行——
   设计 13 前它是 1446 行的 driver 线程，一旦有人把 poll loop 加回，必然超限。
"""

import ast
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent / "src"

#: 设计 13 删除的调度机制符号（定义级守卫，注释提及不算）。
_LEGACY_SYMBOLS = [
    "consume_orphan_artifacts",
    "consume_orphan_done",
    "find_ready_interactive_stories",
    "resume_ready_interactive_stories",
    "_watch_interactive_done_files",
]


def _iter_defs(tree):
    """遍历 AST，产出所有函数/方法定义的名字。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name


def _all_source_files():
    return sorted(_PKG_ROOT.rglob("*.py"))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class TestLegacySchedulingSymbolsNotReintroduced:
    @pytest.mark.parametrize("symbol", _LEGACY_SYMBOLS)
    def test_symbol_not_defined_anywhere(self, symbol):
        """设计 13 删除的调度机制符号不得重新定义为函数。"""
        for path in _all_source_files():
            tree = _parse(path)
            assert symbol not in set(_iter_defs(tree)), (
                f"{symbol} 在 {path} 被重新定义 —— 设计 13 已删除的调度机制不应回潮"
            )

    def test_legacy_symbols_only_in_comments_not_code(self):
        """五个符号即使出现也只在注释/docstring 里（历史引用），不在可执行代码中。"""
        for path in _all_source_files():
            tree = _parse(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    continue  # docstring
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and child.id in _LEGACY_SYMBOLS:
                        raise AssertionError(
                            f"{path}:{child.lineno} 出现可执行符号 {child.id}"
                        )
                    if (
                        isinstance(child, ast.Attribute)
                        and child.attr in _LEGACY_SYMBOLS
                    ):
                        raise AssertionError(
                            f"{path}:{child.lineno} 出现可执行符号 {child.attr}"
                        )


class TestContinueOrchestratorAgentStaysShim:
    """``continue_orchestrator_agent`` 必须保持同步驱动 shim（≤ 30 行代码）。"""

    def _func_node(self):
        planner_path = _PKG_ROOT / "story_lifecycle" / "orchestrator" / "engine" / "planner.py"
        tree = _parse(planner_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "continue_orchestrator_agent":
                return node
        raise AssertionError("planner.py 找不到 continue_orchestrator_agent")

    def test_body_code_lines_within_limit(self):
        """函数体代码行（不含 docstring/空行）≤ 30 行——防 poll loop 加回。

        设计 13 前该函数是 1446 行的 driver 线程；现在只是同步 shim（CLI/
        swebench/测试用），内部驱动同一套 executors/handlers/judge 机制。
        """
        fn = self._func_node()
        body = fn.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        src = Path(
            _PKG_ROOT / "story_lifecycle" / "orchestrator" / "engine" / "planner.py"
        ).read_text(encoding="utf-8").splitlines()
        code_lines = 0
        for stmt in body:
            # 逐行数代码行（跳过空行/纯注释/docstring 行）
            for lineno in range(stmt.lineno - 1, stmt.end_lineno):
                line = src[lineno].strip()
                if line and not line.startswith("#"):
                    code_lines += 1
        assert code_lines <= 30, (
            f"continue_orchestrator_agent 函数体 {code_lines} 行 > 30 —— "
            "poll loop 被偷偷加回（设计 13 已删，编排线程是唯一调度入口）"
        )
