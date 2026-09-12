"""WP-F §8.1 写侧 — RUN 跑测新坑摄入测试(story pitfall import)。

红线(设计 8.1):
- 「本轮新坑(候选回写)」表逐行 → FailureEntry(category=run-pitfall,
  tags=[story_key, "run-pitfall"],source_refs=[RUN md 绝对路径]);
- malformed 行 skip + WARN,绝不让整批失败;
- 幂等:同文件重复导入原地更新,条目数不变;INDEX.json 刷新可检索;
- 真实文件 docs/test-runs/RUN-tapd-1069389-20260908.md 导入 6 条。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

REPO_PKG_DIR = Path(__file__).resolve().parents[1]
REAL_RUN_MD = REPO_PKG_DIR / "docs" / "test-runs" / "RUN-tapd-1069389-20260908.md"
REAL_RUN_MD_1069471 = REPO_PKG_DIR / "docs" / "test-runs" / "RUN-tapd-1069471-20260904.md"

SYNTHETIC_RUN_MD = """# RUN — tapd-1144381896001069123 (2026-09-12)

## 配置

| 字段 | 值 |
|---|---|
| Story key | `tapd-1144381896001069123` |

## 本轮新坑（候选回写）

| 坑 | 规则 |
|---|---|
| 联系人落表坑 | 三方返回要落表,别只落日志 |
| 第二坑:枚举大小写 | 新 journey 前先实查存量数据的大小写 |
| 只有一列的坏行 |

## 其他节

| 坑 | 规则 |
|---|---|
| 不在新坑节里 | 不应被采集 |
"""


def _ensure_knowledge_importable():
    ksrc = REPO_PKG_DIR.parent / "knowledge" / "src"
    if ksrc.is_dir() and str(ksrc) not in sys.path:
        sys.path.insert(0, str(ksrc))
    import knowledge  # noqa: F401

    return knowledge


@pytest.fixture(autouse=True)
def _isolated_kroot(tmp_path, monkeypatch):
    """知识根指向 tmp(config 置空防真机 config.yaml 的 knowledge_root 抢先)。"""
    import story_lifecycle.infra.config as _cfg_mod

    monkeypatch.setattr(_cfg_mod, "get_config", lambda: {})
    monkeypatch.setenv("STORY_KNOWLEDGE_ROOT", str(tmp_path / "kroot"))
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")


@pytest.fixture
def kroot(tmp_path) -> Path:
    return tmp_path / "kroot"


@pytest.fixture
def run_md(tmp_path) -> Path:
    p = tmp_path / "RUN-tapd-1144381896001069123-20260912.md"
    p.write_text(SYNTHETIC_RUN_MD, encoding="utf-8")
    return p


# ---- 基础解析:行 → 条目 + 容错 ----


def test_parse_extracts_two_rows_and_warns_on_malformed(run_md, caplog):
    """2 行有效 → 2 条;1 行缺列 → skip + warning,不炸。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        parse_run_pitfalls,
    )

    with caplog.at_level(logging.WARNING, logger="story_lifecycle.knowledge.knowledge_store.run_pitfalls"):
        entries = parse_run_pitfalls(run_md.read_text(encoding="utf-8"), source_path=run_md)

    assert len(entries) == 2
    by_title = {e.title: e for e in entries}
    assert set(by_title) == {"联系人落表坑", "第二坑:枚举大小写"}
    assert any("格式错误" in r.message for r in caplog.records)
    # 不在新坑节里的同名表不采集
    assert "不在新坑节里" not in by_title


def test_entry_fields_category_tags_source_refs_id(run_md):
    """category/tags/source_refs/id 按 8.1 契约。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        PITFALL_CATEGORY,
        pitfall_entry_id,
        parse_run_pitfalls,
    )

    entries = parse_run_pitfalls(run_md.read_text(encoding="utf-8"), source_path=run_md)
    for e in entries:
        assert e.type == "failure"
        assert e.category == PITFALL_CATEGORY == "run-pitfall"
        assert e.tags == ["tapd-1144381896001069123", "run-pitfall"]
        assert e.source_refs == [str(run_md.resolve())]
        assert e.detail
        assert e.id == pitfall_entry_id("tapd-1144381896001069123", e.title)
        assert e.id.startswith("failure:run-pitfall-")


def test_heading_variants_and_story_key_extraction(tmp_path):
    """「本轮新坑」(无后缀)、「新坑与经验」、编号列表变体都能解析;key 取标题行。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        extract_run_story_key,
        parse_run_pitfall_rows,
    )

    md = """# RUN — tapd-999 (2026-09-01)

## 本轮新坑

| 坑 | 规则 |
|---|---|
| A坑 | A规则 |

## 新坑与经验（已回写 skill RUNS.md）

| 坑/经验 | 处理 |
|---|---|
| B坑 | B规则 |

## 新坑 → 速查

1. **C坑标题**：C规则说明
"""
    rows, skipped = parse_run_pitfall_rows(md, source="RUN-tapd-999-x.md")
    assert extract_run_story_key(md) == "tapd-999"
    assert rows == [("A坑", "A规则"), ("B坑", "B规则"), ("C坑标题", "C规则说明")]
    assert skipped == 0

    # 无标题行 → 空 key(调用方用文件名 stem 兜底)
    assert extract_run_story_key("## 新坑\n\n没有标题行") == ""


def test_no_pitfall_section_yields_zero(tmp_path):
    """没有新坑节的 RUN 文件(如 RUN-tapd-1069654-20260907.md)→ 0 条,不报错。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        parse_run_pitfalls,
    )

    md = "# RUN — tapd-1069654 (2026-09-07)\n\n## 时间线\n\n| 时间 | 事件 |\n|---|---|\n"
    assert parse_run_pitfalls(md, source_path="RUN-tapd-1069654.md") == []


# ---- 加粗列表行变体(- **新坑入库**: + 坑表紧随,无 # 标题) ----


def test_bold_bullet_xinkeng_table_parses_and_guard_rejects():
    """加粗列表行小节变体:紧跟坑表 → 采集;无表/表在窗外 → 不开小节;
    标题节里再嵌加粗行 → 只算一张表,不重复计数。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        parse_run_pitfall_rows,
    )

    # 正:表头在加粗行的下一行(RUN-tapd-1069471 实测形态,缩进表)
    pos = (
        "# RUN — tapd-9471 (2026-09-04)\n"
        "\n"
        "- **新坑入库**：\n"
        "  | 坑 | 规则 |\n"
        "  |---|---|\n"
        "  | A坑 | A规则 |\n"
        "  | B坑 | B规则 |\n"
    )
    rows, skipped = parse_run_pitfall_rows(pos, source="pos.md")
    assert rows == [("A坑", "A规则"), ("B坑", "B规则")]
    assert skipped == 0

    # 正(变体):加粗行与表之间隔一个空行(markdown 常见),仍在窗口内
    pos_blank = (
        "# RUN — tapd-9472 (2026-09-05)\n"
        "\n"
        "- **新坑入库**：\n"
        "\n"
        "| 坑 | 规则 |\n"
        "|---|---|\n"
        "| C坑 | C规则 |\n"
    )
    rows_b, _ = parse_run_pitfall_rows(pos_blank, source="pos_blank.md")
    assert rows_b == [("C坑", "C规则")]

    # 防(误触发防线):加粗新坑行后无表 → 不开小节
    neg_no_table = (
        "# RUN — tapd-guard (2026-09-06)\n"
        "\n"
        "- **新坑复盘**：见上文,本轮零新坑\n"
    )
    assert parse_run_pitfall_rows(neg_no_table, source="n.md") == ([], 0)

    # 防:坑表在窗口(3 行)之外 → 不算加粗行的小节,行不被采集
    neg_beyond_window = (
        "# RUN — tapd-guard2 (2026-09-07)\n"
        "\n"
        "- **新坑复盘**：见上文时间线,本轮无新表\n"
        "\n"
        "> 说明:历史坑已归档,不再重复。\n"
        "\n"
        "| 坑 | 规则 |\n"
        "|---|---|\n"
        "| 不该采集 | 加粗行 3 行内没跟坑表 |\n"
    )
    rows_w, _ = parse_run_pitfall_rows(neg_beyond_window, source="w.md")
    assert rows_w == []

    # 标题节内嵌加粗行+表:行属于外层小节,只采集一次
    nested = (
        "# RUN — tapd-nested (2026-09-08)\n"
        "\n"
        "## 本轮新坑（候选回写）\n"
        "\n"
        "- **新坑入库**：\n"
        "  | 坑 | 规则 |\n"
        "  |---|---|\n"
        "  | X坑 | X规则 |\n"
    )
    rows_n, _ = parse_run_pitfall_rows(nested, source="nested.md")
    assert rows_n == [("X坑", "X规则")]


def test_bold_bullet_variant_imports_rows(tmp_path, kroot):
    """加粗列表行变体走完整导入链:行落 failures json + INDEX 可检索。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )
    from knowledge import KnowledgeIndex

    md = tmp_path / "RUN-tapd-9471-20260904.md"
    md.write_text(
        "# RUN — tapd-9471 (2026-09-04)\n"
        "\n"
        "## 落地处置\n"
        "\n"
        "- **新坑入库**：\n"
        "  | 坑 | 规则 |\n"
        "  |---|---|\n"
        "  | 红锚点假绿 | 查值必须配结构化 equals 断言 |\n"
        "  | git stash 误弹 | worktree 无改动时 pop 会弹旧条目 |\n",
        encoding="utf-8",
    )
    summary = import_run_pitfalls(md, root=kroot)
    assert summary["imported"] == 2 and summary["updated"] == 0 and summary["skipped"] == 0

    idx = KnowledgeIndex(str(kroot))
    pitfalls = [e for e in idx.all() if e.category == "run-pitfall"]
    assert len(pitfalls) == 2
    for e in pitfalls:
        assert e.tags == ["tapd-9471", "run-pitfall"]
        assert e.source_refs == [str(md.resolve())]
    assert idx.retrieve(query="红锚点", top_k=3)[0].category == "run-pitfall"


# ---- 导入:持久化 + 幂等 + INDEX 刷新 ----


def test_import_writes_failures_json_and_index(run_md, kroot):
    """导入 → failures/failure-knowledge.json + INDEX.json 都落盘,可检索。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )
    from knowledge import KnowledgeIndex

    summary = import_run_pitfalls(run_md, root=kroot)
    assert summary["imported"] == 2
    assert summary["updated"] == 0
    assert (kroot / "failures" / "failure-knowledge.json").exists()
    assert (kroot / "INDEX.json").exists()

    idx = KnowledgeIndex(str(kroot))
    hits = idx.retrieve(query="联系人落表", top_k=5)
    assert hits, "INDEX 刷新后必须能检索到新坑"
    assert hits[0].source_refs == [str(run_md.resolve())]


def test_reimport_is_idempotent_update_in_place(run_md, kroot):
    """重复导入同一文件 → 全走 update 路径,条目总数不变。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )
    from knowledge import KnowledgeIndex

    first = import_run_pitfalls(run_md, root=kroot)
    assert first["imported"] == 2 and first["updated"] == 0

    second = import_run_pitfalls(run_md, root=kroot)
    assert second["imported"] == 0 and second["updated"] == 2
    assert second["skipped"] == 1  # malformed 行每次都 skip + warn,但绝不炸

    idx = KnowledgeIndex(str(kroot))
    failures = [e for e in idx.all() if e.category == "run-pitfall"]
    assert len(failures) == 2, "重复导入不得产生重复条目"


def test_dir_input_walks_md_recursively(tmp_path, kroot):
    """目录输入 → 递归扫 *.md,汇总各文件计数。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )

    sub = tmp_path / "runs" / "nested"
    sub.mkdir(parents=True)
    (sub / "RUN-a.md").write_text(SYNTHETIC_RUN_MD, encoding="utf-8")
    (tmp_path / "runs" / "note.txt").write_text("不是 md", encoding="utf-8")

    summary = import_run_pitfalls(tmp_path / "runs", root=kroot)
    assert [Path(f["file"]).name for f in summary["files"]] == ["RUN-a.md"]
    assert summary["imported"] == 2


def test_import_default_root_via_env(run_md, kroot):
    """root 缺省 → resolve_knowledge_root(env STORY_KNOWLEDGE_ROOT)。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )

    summary = import_run_pitfalls(run_md)
    assert summary["failures_path"] == str(kroot / "failures" / "failure-knowledge.json")


def test_import_missing_target_raises_chinese(tmp_path):
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )

    with pytest.raises(FileNotFoundError, match="路径不存在"):
        import_run_pitfalls(tmp_path / "nope.md", root=tmp_path / "kroot")


def test_failure_json_payload_shape(run_md, kroot):
    """落盘 JSON 保持 knowledge 包约定形状:{"version":1,"failures":[...]}。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )

    import_run_pitfalls(run_md, root=kroot)
    payload = json.loads(
        (kroot / "failures" / "failure-knowledge.json").read_text(encoding="utf-8")
    )
    assert payload["version"] == 1
    assert len(payload["failures"]) == 2
    item = payload["failures"][0]
    assert item["category"] == "run-pitfall"
    assert item["created_at"] and item["updated_at"]


# ---- 真实文件验收(设计 8.1 验收线) ----


@pytest.mark.skipif(not REAL_RUN_MD.exists(), reason="real RUN md not in checkout")
def test_real_run_file_imports_six_entries(kroot):
    """真实 RUN-tapd-1069389-20260908.md → 恰好 6 条(表 6 行);重复导入 no-op。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )
    from knowledge import KnowledgeIndex

    first = import_run_pitfalls(REAL_RUN_MD, root=kroot)
    assert first["imported"] == 6, f"真实文件应导入 6 条,实际 {first['imported']}"

    second = import_run_pitfalls(REAL_RUN_MD, root=kroot)
    assert second["imported"] == 0 and second["updated"] == 6

    idx = KnowledgeIndex(str(kroot))
    pitfalls = [e for e in idx.all() if e.category == "run-pitfall"]
    assert len(pitfalls) == 6
    for e in pitfalls:
        assert e.tags == ["tapd-1069389", "run-pitfall"]
        assert e.source_refs == [str(REAL_RUN_MD.resolve())]
    # 飞轮读侧可命中(标题词检索;detail 不参与 retrieve 打分,故用坑标题关键词)
    hits = idx.retrieve(query="三方可通性", top_k=3)
    assert hits and hits[0].category == "run-pitfall"


@pytest.mark.skipif(not REAL_RUN_MD_1069471.exists(), reason="real RUN md not in checkout")
def test_real_run_1069471_bold_bullet_table_imports_six(kroot):
    """真实 RUN-tapd-1069471-20260904.md(新坑表在加粗列表行「- **新坑入库**：」
    下,无 # 标题)→ 恰好其表 6 行;重复导入幂等。曾因只认标题小节而静默 0 条。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )
    from knowledge import KnowledgeIndex

    first = import_run_pitfalls(REAL_RUN_MD_1069471, root=kroot)
    assert first["imported"] == 6, f"真实文件应导入 6 条,实际 {first['imported']}"
    assert first["skipped"] == 0

    second = import_run_pitfalls(REAL_RUN_MD_1069471, root=kroot)
    assert second["imported"] == 0 and second["updated"] == 6

    idx = KnowledgeIndex(str(kroot))
    pitfalls = [e for e in idx.all() if e.category == "run-pitfall"]
    assert len(pitfalls) == 6
    for e in pitfalls:
        assert e.tags == ["tapd-1069471", "run-pitfall"]
        assert e.source_refs == [str(REAL_RUN_MD_1069471.resolve())]
    # 读侧可命中其中一坑标题
    hits = idx.retrieve(query="git stash", top_k=3)
    assert hits and any(e.title == "git stash 误弹历史 stash" for e in hits)


# ---- CLI 装配 ----


def test_cli_pitfall_import_registered():
    """`story pitfall import` 子命令已挂到主 CLI(注册面)。"""
    from click.testing import CliRunner

    from story_lifecycle.entry.cli.main import cli

    result = CliRunner().invoke(cli, ["pitfall", "--help"])
    assert result.exit_code == 0
    assert "import" in result.output
