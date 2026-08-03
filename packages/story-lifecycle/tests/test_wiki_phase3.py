"""Phase 3 wiki 测试 — 11-workspace-entity-design.md §4/§5。

覆盖:wiki 管线(human 直接生效 / AI-probe draft / review merge-reject)、
CodeScanProbe(L1 + 聚合统计 PII)、probe loader、generate_wiki_drafts、
stale 检测(重跑 probe 对比 + git 语义,无 mtime)、agent 注入(summary+related
只取/降权/stale 标注)、API 端点。
"""

import json
import subprocess
import sys
from pathlib import Path

# 单包路径跑 pytest 时 rootdir = packages/story-lifecycle(pythonpath=["src"]),
# knowledge 包不在 sys.path → 知识层静默降级导致测试全空。手动补上 knowledge/src
# (全量 pytest 从仓库根跑时 rootdir 是仓库根,此处 insert 无副作用)。
_KNOWLEDGE_SRC = Path(__file__).resolve().parents[2] / "knowledge" / "src"
if str(_KNOWLEDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_KNOWLEDGE_SRC))

import pytest  # noqa: E402

from story_lifecycle.infra.db import models as db  # noqa: E402
from story_lifecycle.knowledge import wiki_pipeline as wiki  # noqa: E402
from story_lifecycle.knowledge.wiki_probes import (  # noqa: E402
    load_wiki_probes,
)
from story_lifecycle.knowledge.wiki_probes.base import (  # noqa: E402
    BaseWikiProbe,
    Evidence,
)
from story_lifecycle.knowledge.wiki_probes.code_scan import (  # noqa: E402
    CodeScanProbe,
)


@pytest.fixture
def knowledge_root(tmp_path) -> Path:
    kroot = tmp_path / "ws" / ".story" / "knowledge"
    (kroot / "wiki").mkdir(parents=True)
    return kroot


def _git_repo(root: Path) -> None:
    """初始化 git 仓库并做首次 commit。"""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(root), check=True)
    (root / ".gitkeep").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(root), check=True)


# -------- wiki 管线:§4.3 人写 vs AI 写 --------


class TestWikiPipeline:
    def test_human_write_takes_effect_directly(self, knowledge_root):
        entry = wiki.save_wiki_entry(
            knowledge_root,
            title="授信域概述",
            content="# 授信域概述\n\n正文",
            source="human",
            summary="授信域覆盖申请到动支",
        )
        assert entry["review_state"] == "merged"
        assert entry["verified_at"]
        assert entry["source"] == "human"
        # 落盘 + 进 INDEX
        assert (knowledge_root / "wiki" / "wiki-page.md").exists()
        assert list_wiki_ids(knowledge_root) == [entry["id"]]
        assert entry["id"] in index_ids(knowledge_root)

    def test_ai_probe_output_is_draft(self, knowledge_root):
        entry = wiki.save_wiki_entry(
            knowledge_root,
            title="接口清单",
            content="正文",
            source="probe:code-scan",
            summary="代码声明 12 个 API",
            evidence_refs=[
                {"probe": "code-scan", "query": "api 注解", "observed_at": "2026-08-03"}
            ],
        )
        assert entry["review_state"] == "draft"
        assert entry["verified_at"] == ""
        assert entry["evidence_refs"][0]["probe"] == "code-scan"

    def test_story_output_is_draft(self, knowledge_root):
        entry = wiki.save_wiki_entry(
            knowledge_root, title="某故事产出", content="c", source="story:ST-1"
        )
        assert entry["review_state"] == "draft"

    def test_review_approve_merges(self, knowledge_root):
        entry = wiki.save_wiki_entry(
            knowledge_root, title="页", content="c", source="probe:p"
        )
        merged = wiki.review_wiki(
            knowledge_root, entry["id"], "approve", reviewer="张三"
        )
        assert merged["review_state"] == "merged"
        assert merged["verified_at"]
        assert merged["reviewed_by"] == "张三"

    def test_review_reject_keeps_draft_with_reason(self, knowledge_root):
        entry = wiki.save_wiki_entry(
            knowledge_root, title="页", content="c", source="probe:p"
        )
        back = wiki.review_wiki(
            knowledge_root, entry["id"], "reject", reason="证据不足"
        )
        assert back["review_state"] == "draft"
        assert back["review_reason"] == "证据不足"

    def test_ai_rewrite_of_merged_page_downgrades_to_draft(self, knowledge_root):
        wiki.save_wiki_entry(knowledge_root, title="页", content="v1", source="human")
        draft = wiki.save_wiki_entry(
            knowledge_root, title="页", content="v2", source="probe:p"
        )
        # I2:AI 不自动覆盖正式知识 — 新版本是 draft,等人工确认
        assert draft["review_state"] == "draft"
        assert draft["content"] == "v2"
        assert draft["verified_at"] == ""

    def test_human_rewrite_stays_merged(self, knowledge_root):
        wiki.save_wiki_entry(knowledge_root, title="页", content="v1", source="human")
        entry = wiki.save_wiki_entry(
            knowledge_root, title="页", content="v2", source="human"
        )
        assert entry["review_state"] == "merged"
        assert entry["content"] == "v2"

    def test_delete_and_filter(self, knowledge_root):
        wiki.save_wiki_entry(knowledge_root, title="页A", content="a", source="human")
        wiki.save_wiki_entry(knowledge_root, title="页B", content="b", source="probe:p")
        assert len(wiki.list_wiki_entries(knowledge_root, "draft")) == 1
        assert len(wiki.list_wiki_entries(knowledge_root, "merged")) == 1
        entry = wiki.list_wiki_entries(knowledge_root, "draft")[0]
        assert wiki.delete_wiki(knowledge_root, entry["id"])
        assert not wiki.delete_wiki(knowledge_root, "wiki:nope")
        assert len(wiki.list_wiki_entries(knowledge_root)) == 1

    def test_invalid_slug_rejected(self, knowledge_root):
        with pytest.raises(ValueError):
            wiki.save_wiki_entry(
                knowledge_root, title="页", content="c", slug="Bad Slug!"
            )


def list_wiki_ids(kroot):
    return [e["id"] for e in wiki.list_wiki_entries(kroot)]


def index_ids(kroot):
    idx = json.loads((Path(kroot) / "INDEX.json").read_text(encoding="utf-8"))
    return {e["id"] for e in idx["entries"] if e["type"] == "wiki"}


# -------- probe:§5 多源探测(核心侧只带 L1) --------


class TestCodeScanProbe:
    def _repo(self, tmp_path) -> Path:
        repo = tmp_path / "hc-credit"
        (repo / "src" / "main" / "java" / "com" / "credit").mkdir(parents=True)
        (
            repo / "src" / "main" / "java" / "com" / "credit" / "OrderController.java"
        ).write_text(
            "@RestController\n"
            '@RequestMapping("/order")\n'
            "public class OrderController {\n"
            '  @GetMapping("/list") public Object list() { return null; }\n'
            '  @PostMapping("/create") public Object create() { return null; }\n'
            "}\n",
            encoding="utf-8",
        )
        (repo / "src" / "main" / "resources").mkdir(parents=True)
        (repo / "src" / "main" / "resources" / "schema.sql").write_text(
            "CREATE TABLE t_loan_order (id BIGINT);\n", encoding="utf-8"
        )
        (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
        return repo

    def test_scan_produces_aggregate_evidence_only(self, tmp_path):
        """I5 PII:只产聚合统计(计数/分布/名称清单),无原始行。"""
        repo = self._repo(tmp_path)
        probe = CodeScanProbe({})
        ws = {"repos": [{"name": "hc-credit", "repo_path": str(repo)}]}
        evs = probe.probe(ws)
        kinds = {e.kind for e in evs}
        assert "api_endpoints" in kinds
        assert "table_definitions" in kinds
        assert "dependency_files" in kinds
        api_ev = next(e for e in evs if e.kind == "api_endpoints")
        assert api_ev.layer == "L1"
        assert api_ev.data["api_count"] == 3  # /order + /list + /create
        assert api_ev.data["methods"] == {"GET": 1, "POST": 1, "REQUEST": 1}
        assert api_ev.query
        assert api_ev.observed_at
        # 所有 data 值都是计数/分布/名称清单 — 无原始用户数据行
        for e in evs:
            assert all(isinstance(v, (int, str, list, dict)) for v in e.data.values())

    def test_divergence_detected(self, tmp_path):
        repo = tmp_path / "svc"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "C.java").write_text(
            '@RequestMapping("/x")\n@GetMapping("/x")\n', encoding="utf-8"
        )
        evs = CodeScanProbe({}).probe(
            {"repos": [{"name": "svc", "repo_path": str(repo)}]}
        )
        div = [e for e in evs if e.kind == "api_divergence"]
        assert div and "/x" in div[0].data["divergent_paths"]

    def test_missing_repo_returns_empty(self, tmp_path):
        evs = CodeScanProbe({}).probe(
            {"repos": [{"repo_path": str(tmp_path / "nope")}]}
        )
        assert evs == []


class TestWikiProbeLoader:
    def test_empty_config_returns_empty(self):
        assert load_wiki_probes({}) == []

    def test_broken_probe_skipped(self):
        probes = load_wiki_probes(
            {"wiki_probes": [{"module": "no.such.module", "class": "X"}]}
        )
        assert probes == []

    def test_duck_type_validation(self):
        probes = load_wiki_probes(
            {
                "wiki_probes": [
                    {
                        "module": "story_lifecycle.knowledge.wiki_probes.code_scan",
                        "class": "CodeScanProbe",
                    }
                ]
            }
        )
        assert len(probes) == 1
        assert isinstance(probes[0], CodeScanProbe)


class _FakeL3Probe(BaseWikiProbe):
    """模拟 hc 侧 L3 数据现实 probe(测试用,本仓库不实现 hc probe)。"""

    def probe(self, workspace: dict) -> list[Evidence]:
        return [
            Evidence(
                layer="L3",
                kind="table_distribution",
                summary="t_loan_order 线上 status 分布:8 态",
                data={"status_count": 8, "statuses": ["A", "B", "C"]},
                query="SELECT status, count(*) ...",
                observed_at="2026-08-03T00:00:00+00:00",
            )
        ]


def _ws_with_repo(tmp_path, name="hc-credit"):
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "A.java").write_text('@GetMapping("/a")\n', encoding="utf-8")
    kroot = tmp_path / ".story" / "knowledge"
    (kroot / "wiki").mkdir(parents=True)
    return {
        "id": 1,
        "name": name,
        "knowledge_root": str(kroot),
        "repos": [{"name": name, "repo_path": str(repo)}],
    }


class TestGenerateWikiDrafts:
    def test_zero_config_gets_l1_skeleton(self, tmp_path):
        """§5.4/§9:不配 probe 时只有 L1 骨架(核心自带 CodeScanProbe)。"""
        ws = _ws_with_repo(tmp_path)
        drafts = wiki.generate_wiki_drafts(ws, config={})
        assert drafts, "零配置也应产出 L1 draft"
        for d in drafts:
            assert d["review_state"] == "draft"
            assert d["source"].startswith("probe:")
            assert d["evidence_refs"][0]["probe"]
        ids = [d["id"] for d in drafts]
        assert any("code-scan" in i for i in ids)

    def test_configured_probe_draft_has_evidence_refs(self, tmp_path):
        """配了 hc probe 后 wiki draft 含 L3 证据(带 evidence_refs)。"""
        ws = _ws_with_repo(tmp_path)
        drafts = wiki.generate_wiki_drafts(
            ws, probes=[CodeScanProbe({}), _FakeL3Probe({})]
        )
        l3 = [d for d in drafts if d["evidence_refs"][0]["probe"] == "fake-l3"]
        assert l3 and l3[0]["probe_snapshot"] == {
            "status_count": 8,
            "statuses": ["A", "B", "C"],
        }
        assert l3[0]["evidence_refs"][0]["query"] == "SELECT status, count(*) ..."

    def test_merged_page_not_overwritten(self, tmp_path):
        ws = _ws_with_repo(tmp_path)
        wiki.generate_wiki_drafts(ws, probes=[_FakeL3Probe({})])
        # 人工把 draft merge 后,再跑不覆盖正式页
        for e in wiki.list_wiki_entries(ws["knowledge_root"], "draft"):
            wiki.review_wiki(ws["knowledge_root"], e["id"], "approve")
        count_before = len(wiki.list_wiki_entries(ws["knowledge_root"], "merged"))
        wiki.generate_wiki_drafts(ws, probes=[_FakeL3Probe({})])
        assert (
            len(wiki.list_wiki_entries(ws["knowledge_root"], "merged")) == count_before
        )


# -------- stale:§5.3 重跑 probe 对比 + git 语义 --------


class _MutableL3Probe(_FakeL3Probe):
    """数据可变的 L3 probe:生成时给旧快照,stale 检查时给新数据(同类名同 tag)。"""

    def __init__(self, config: dict, data: dict):
        super().__init__(config)
        self._data = data

    def probe(self, workspace: dict) -> list[Evidence]:
        return [
            Evidence(
                layer="L3",
                kind="table_distribution",
                summary=f"线上 status 分布:{len(self._data.get('statuses', []))} 态",
                data=self._data,
                query="SELECT status, count(*) ...",
                observed_at="2026-08-03T00:00:00+00:00",
            )
        ]


class TestWikiStale:
    def test_probe_rerun_comparison_flags_stale(self, tmp_path):
        """probe 聚合数据变了 → 证据过期(重跑对比,不用 mtime)。"""
        ws = _ws_with_repo(tmp_path)
        probe = _MutableL3Probe({}, {"status_count": 8, "statuses": ["A", "B", "C"]})
        wiki.generate_wiki_drafts(ws, probes=[probe])
        # 代码改了 → probe 重跑数据变 9 态
        (Path(ws["repos"][0]["repo_path"]) / "src" / "B.java").write_text(
            '@GetMapping("/b")\n', encoding="utf-8"
        )
        probe._data = {"status_count": 9, "statuses": ["A", "B", "C", "D"]}

        results = wiki.check_wiki_stale(ws, probes=[probe])
        stale = [r for r in results if r["stale"]]
        assert stale, results
        assert any("probe 重跑对比" in r for rr in results for r in rr["reasons"])

    def test_no_mtime_false_positive(self, tmp_path):
        """只 touch 文件(mtime 变)但没有 git/数据变化 → 不过期。"""
        ws = _ws_with_repo(tmp_path)
        wiki.generate_wiki_drafts(ws, probes=[_FakeL3Probe({})])
        # touch 一个源文件但不改内容
        src = Path(ws["repos"][0]["repo_path"]) / "src" / "A.java"
        src.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        results = wiki.check_wiki_stale(ws, probes=[_FakeL3Probe({})])
        assert all(not r["stale"] for r in results)

    def test_git_change_after_verified_at_flags_stale(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        _git_repo(root)
        kroot = root / ".story" / "knowledge"
        (kroot / "wiki").mkdir(parents=True)
        # 人写条目,关联代码文件(verified_at 稍后改写为过去)
        wiki.save_wiki_entry(
            kroot,
            title="稳定页",
            content="c",
            source="human",
            source_refs=["svc/Code.java"],
            slug="stable-page",
        )
        # 代码在 verified_at 之后变更
        svc = root / "svc"
        svc.mkdir()
        (svc / "Code.java").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        subprocess.run(
            ["git", "commit", "-qm", "code change"], cwd=str(root), check=True
        )
        # 重写 verified_at 为过去(模拟很久前确认)
        entry = wiki.get_wiki_entry(kroot, "wiki:stable-page")
        entry["verified_at"] = "2000-01-01T00:00:00+00:00"
        wiki._write_entry_file(kroot, entry)
        ws = {"knowledge_root": str(kroot), "repos": []}
        results = wiki.check_wiki_stale(ws, probes=[])
        r = next(x for x in results if x["wiki_id"] == "wiki:stable-page")
        assert r["stale"]
        assert any("关联代码" in rr for rr in r["reasons"])


# -------- agent 注入:§4.2 双读者 --------


class TestWikiInjection:
    def _provider(self, tmp_path, monkeypatch, kroot):
        from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

        monkeypatch.setattr(kp, "_KNOWLEDGE_ROOT", Path(kroot))
        return kp.KnowledgeContextProvider(), kp

    def test_injection_only_summary_and_related_merged_only(
        self, tmp_path, monkeypatch
    ):
        kroot = tmp_path / ".story" / "knowledge"
        (kroot / "wiki").mkdir(parents=True)
        wiki.save_wiki_entry(
            kroot,
            title="授信域概述",
            content="非常长的正文,不应注入…" * 50,
            source="human",
            summary="授信域覆盖申请到动支",
            related=["scenario:borrow-flow"],
            slug="credit-domain",
        )
        wiki.save_wiki_entry(
            kroot,
            title="未确认页",
            content="c",
            source="probe:p",
            summary="未确认摘要",
            slug="unconfirmed",
        )
        provider, _ = self._provider(tmp_path, monkeypatch, kroot)
        section = provider._build_wiki_summary_section("credit-limit")
        assert "授信域覆盖申请到动支" in section
        assert "scenario:borrow-flow" in section
        # draft 不注入;正文全文不注入
        assert "未确认摘要" not in section
        assert "非常长的正文" not in section
        assert section.index("### Wiki 摘要") >= 0

    def test_wiki_section_after_knowledge_section(self, tmp_path, monkeypatch):
        """降权:wiki 摘要段在 playbook/scenario/failure 知识段之后。"""
        kroot = tmp_path / ".story" / "knowledge"
        (kroot / "wiki").mkdir(parents=True)
        # scenario 域用 core-business 命中检索(credit-limit 的首个映射域)
        (kroot / "scenarios" / "core-business").mkdir(parents=True)
        (kroot / "scenarios" / "core-business" / "borrow.md").write_text(
            "---\nid: scenario:borrow-flow\ntitle: 借款流程\n---\n# 借款流程\n",
            encoding="utf-8",
        )
        wiki.save_wiki_entry(
            kroot, title="页", content="c", source="human", summary="s"
        )
        # get_context 需要 task_type:在 DB 里种一条 story(context_json.task_type)
        db.create_story("ST-1", "story", "/ws", current_stage="design")
        db.update_context("ST-1", "task_type", "credit-limit")
        provider, _ = self._provider(tmp_path, monkeypatch, kroot)
        ctx = provider.get_context("ST-1", "/ws", "design") or ""
        k_idx = ctx.find("### 知识库")
        w_idx = ctx.find("### Wiki 摘要")
        assert k_idx >= 0, ctx
        assert w_idx >= 0 and w_idx > k_idx

    def test_stale_annotation_in_injection(self, tmp_path, monkeypatch):
        root = tmp_path / "wsroot"
        root.mkdir()
        _git_repo(root)
        kroot = root / ".story" / "knowledge"
        (kroot / "wiki").mkdir(parents=True)
        svc = root / "svc"
        svc.mkdir()
        (svc / "Code.java").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        subprocess.run(["git", "commit", "-qm", "add code"], cwd=str(root), check=True)
        wiki.save_wiki_entry(
            kroot,
            title="过期页",
            content="c",
            source="human",
            summary="s",
            source_refs=["svc/Code.java"],
            slug="stale-page",
        )
        entry = wiki.get_wiki_entry(kroot, "wiki:stale-page")
        entry["verified_at"] = "2000-01-01T00:00:00+00:00"
        wiki._write_entry_file(kroot, entry)
        provider, _ = self._provider(tmp_path, monkeypatch, kroot)
        section = provider._build_wiki_summary_section("credit-limit")
        assert "可能过期" in section
        assert "以代码为准" in section

    def test_no_wiki_no_section(self, tmp_path, monkeypatch):
        kroot = tmp_path / ".story" / "knowledge"
        (kroot / "wiki").mkdir(parents=True)
        provider, _ = self._provider(tmp_path, monkeypatch, kroot)
        assert provider._build_wiki_summary_section("credit-limit") == ""


# -------- API --------


@pytest.fixture
def api_client(isolated_story_home):
    from story_lifecycle.orchestrator.service.api import app
    from fastapi.testclient import TestClient

    return TestClient(app)


class TestWikiAPI:
    def _seed_workspace(self, tmp_path) -> str:
        """建 workspace + 注册 repo + 知识根,返回 slug。"""
        repo = tmp_path / "hc-credit"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "A.java").write_text('@GetMapping("/a")\n', encoding="utf-8")
        kroot = tmp_path / ".story" / "knowledge"
        (kroot / "wiki").mkdir(parents=True)
        from story_lifecycle.orchestrator.workspace import workspace_registry as wr
        from story_lifecycle.orchestrator.workspace.project_registry import (
            register_project,
        )

        ws = wr.create_workspace("Wiki 域", slug="wiki-domain")
        proj = register_project(name="hc-credit", repo_path=str(repo))
        db.update_project(proj["id"], workspace_id=ws["id"])
        db.update_workspace(ws["id"], knowledge_root=str(kroot))
        return "wiki-domain"

    def test_save_human_then_review_draft(self, api_client, tmp_path):
        slug = self._seed_workspace(tmp_path)
        # 人写 → 直接 merged
        resp = api_client.post(
            f"/api/workspace-entities/{slug}/wiki",
            json={
                "title": "概览",
                "content": "正文",
                "source": "human",
                "summary": "s",
                "slug": "overview",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["review_state"] == "merged"
        # AI 产出 → draft,再 approve
        resp = api_client.post(
            f"/api/workspace-entities/{slug}/wiki",
            json={
                "title": "接口清单",
                "content": "c",
                "source": "probe:code-scan",
                "summary": "s2",
                "slug": "api-list",
                "evidence_refs": [
                    {"probe": "code-scan", "query": "q", "observed_at": "2026-08-03"}
                ],
            },
        )
        assert resp.status_code == 200
        wiki_id = resp.json()["id"]
        assert resp.json()["review_state"] == "draft"
        # review approve
        resp = api_client.post(
            f"/api/workspace-entities/{slug}/wiki/{wiki_id}/review",
            json={"decision": "approve", "reviewer": "user"},
        )
        assert resp.status_code == 200
        assert resp.json()["review_state"] == "merged"
        # list
        resp = api_client.get(f"/api/workspace-entities/{slug}/wiki")
        assert resp.status_code == 200
        assert len(resp.json()["wiki"]) == 2
        resp = api_client.get(f"/api/workspace-entities/{slug}/wiki?review_state=draft")
        assert resp.json()["wiki"] == []

    def test_reject_records_reason(self, api_client, tmp_path):
        slug = self._seed_workspace(tmp_path)
        resp = api_client.post(
            f"/api/workspace-entities/{slug}/wiki",
            json={"title": "d", "content": "c", "source": "probe:p"},
        )
        wiki_id = resp.json()["id"]
        resp = api_client.post(
            f"/api/workspace-entities/{slug}/wiki/{wiki_id}/review",
            json={"decision": "reject", "reason": "证据不足"},
        )
        assert resp.status_code == 200
        assert resp.json()["review_state"] == "draft"
        assert resp.json()["review_reason"] == "证据不足"

    def test_generate_endpoint(self, api_client, tmp_path):
        slug = self._seed_workspace(tmp_path)
        resp = api_client.post(f"/api/workspace-entities/{slug}/wiki/generate")
        assert resp.status_code == 200
        assert resp.json()["created"] >= 1
        resp = api_client.get(f"/api/workspace-entities/{slug}/wiki")
        assert all(e["review_state"] == "draft" for e in resp.json()["wiki"])

    def test_workspace_detail_includes_wiki(self, api_client, tmp_path):
        slug = self._seed_workspace(tmp_path)
        api_client.post(
            f"/api/workspace-entities/{slug}/wiki",
            json={"title": "概览", "content": "正文", "source": "human"},
        )
        resp = api_client.get(f"/api/workspace-entities/{slug}")
        assert resp.status_code == 200
        wiki_entries = resp.json()["wiki"]
        assert len(wiki_entries) == 1
        assert wiki_entries[0]["review_state"] == "merged"

    def test_no_knowledge_root_returns_400(self, api_client):
        from story_lifecycle.orchestrator.workspace import workspace_registry as wr

        wr.create_workspace("无知识根", slug="no-kroot")
        resp = api_client.get("/api/workspace-entities/no-kroot/wiki")
        assert resp.status_code == 400
