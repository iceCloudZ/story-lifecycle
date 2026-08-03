"""WikiEntry(type: wiki)解析 + INDEX 索引测试(11-workspace-entity-design.md §4/§5)。

只测新追加的 WikiEntry/parse_wiki 行为;既有条目类型行为不受影响。
"""

from pathlib import Path

from knowledge.generator import generate_index, write_index
from knowledge.parser import parse_wiki
from knowledge.index import _entry_from_dict


def _make_wiki(
    knowledge_dir: Path,
    filename: str = "credit-domain.md",
    body: str = "# 授信域概述\n\n正文叙述...",
):
    wiki = knowledge_dir / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(
        "---\n"
        "id: wiki:credit-domain\n"
        "type: wiki\n"
        "title: 授信域概述\n"
        "summary: 授信域覆盖申请到动支,核心服务 hc-credit/hc-risk\n"
        "source: probe:code_scan\n"
        "review_state: draft\n"
        "evidence_refs:\n"
        "  - probe: code_scan\n"
        "    query: api 注解扫描\n"
        "related: [scenario:borrow-flow]\n"
        "probe_snapshot: {api_count: 12}\n"
        "---\n" + body,
        encoding="utf-8",
    )
    return path


def test_parse_wiki_frontmatter_and_body(tmp_path):
    kdir = tmp_path / ".story" / "knowledge"
    path = _make_wiki(kdir)
    entry = parse_wiki(str(path), "wiki/credit-domain.md")

    assert entry.id == "wiki:credit-domain"
    assert entry.type == "wiki"
    assert entry.title == "授信域概述"
    assert entry.summary == "授信域覆盖申请到动支,核心服务 hc-credit/hc-risk"
    assert entry.source == "probe:code_scan"
    assert entry.review_state == "draft"
    assert entry.evidence_refs == [{"probe": "code_scan", "query": "api 注解扫描"}]
    assert entry.related == ["scenario:borrow-flow"]
    assert entry.probe_snapshot == {"api_count": 12}
    assert entry.content.startswith("# 授信域概述")
    # frontmatter 不进正文
    assert "summary:" not in entry.content


def test_parse_wiki_defaults(tmp_path):
    """缺省:source=human(直接生效语义)、review_state=draft、无正文可读。"""
    kdir = tmp_path / ".story" / "knowledge"
    wiki = kdir / "wiki"
    wiki.mkdir(parents=True)
    path = wiki / "bare.md"
    path.write_text("---\ntitle: 裸页\n---\n", encoding="utf-8")
    entry = parse_wiki(str(path), "wiki/bare.md")
    assert entry.id == "wiki:bare"
    assert entry.source == "human"
    assert entry.review_state == "draft"
    assert entry.content == ""


def test_generate_index_includes_wiki_entries(tmp_path):
    kdir = tmp_path / ".story" / "knowledge"
    _make_wiki(kdir)
    payload = generate_index(str(kdir))
    wiki_entries = [e for e in payload["entries"] if e["type"] == "wiki"]
    assert len(wiki_entries) == 1
    w = wiki_entries[0]
    assert w["id"] == "wiki:credit-domain"
    assert w["review_state"] == "draft"
    assert w["summary"]
    assert w["evidence_refs"][0]["probe"] == "code_scan"
    assert w["related"] == ["scenario:borrow-flow"]
    # content(正文)进 INDEX,供人读渲染
    assert w["content"].startswith("# 授信域概述")


def test_index_roundtrip_hydrates_wiki_entry(tmp_path):
    """INDEX.json 写入再读回 → _entry_from_dict 还原 WikiEntry。"""
    kdir = tmp_path / ".story" / "knowledge"
    _make_wiki(kdir)
    write_index(str(kdir))

    import json

    payload = json.loads((kdir / "INDEX.json").read_text(encoding="utf-8"))
    entry = _entry_from_dict([e for e in payload["entries"] if e["type"] == "wiki"][0])
    assert type(entry).__name__ == "WikiEntry"
    assert entry.summary
    assert entry.review_state == "draft"
    assert entry.evidence_refs


def test_existing_entry_types_unaffected(tmp_path):
    """既有 scenario/playbook 解析行为不变(wiki 追加是加分支,不是改分支)。"""
    kdir = tmp_path / ".story" / "knowledge"
    scenarios = kdir / "scenarios" / "core-borrow"
    scenarios.mkdir(parents=True)
    (scenarios / "borrow.md").write_text(
        "---\nid: scenario:borrow-flow\ntitle: 借款流程\n---\n# 借款流程\n",
        encoding="utf-8",
    )
    from knowledge.parser import parse_scenario

    entry = parse_scenario(
        str(scenarios / "borrow.md"), "scenarios/core-borrow/borrow.md"
    )
    assert entry.id == "scenario:borrow-flow"
    assert entry.type == "scenario"
