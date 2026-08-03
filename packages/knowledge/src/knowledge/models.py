"""Dataclasses for the unified knowledge schema."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnowledgeEntry:
    """Base for all knowledge entities."""

    id: str
    type: str
    title: str
    source: str  # "static" | "dynamic"
    domain: str = ""
    status: str = "extracted"
    trigger: dict[str, Any] = field(default_factory=dict)
    must_read: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    path: str = ""
    links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "source": self.source,
            "domain": self.domain,
            "status": self.status,
            "trigger": self.trigger,
            "must_read": self.must_read,
            "roles": self.roles,
            "tags": self.tags,
            "source_refs": self.source_refs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": self.path,
            "links": self.links,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeEntry":
        base = {
            "id": data["id"],
            "type": data["type"],
            "title": data["title"],
            "source": data["source"],
        }
        optional = {
            k: data.get(k, v)
            for k, v in [
                ("domain", ""),
                ("status", "extracted"),
                ("trigger", {}),
                ("must_read", []),
                ("roles", []),
                ("tags", []),
                ("source_refs", []),
                ("created_at", ""),
                ("updated_at", ""),
                ("path", ""),
                ("links", []),
            ]
        }
        return cls(**base, **optional)


@dataclass
class ScenarioEntry(KnowledgeEntry):
    """Static business-structure knowledge."""

    participating_services: list[str] = field(default_factory=list)
    main_flow: list[str] = field(default_factory=list)
    apis: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    mq_topics: list[str] = field(default_factory=list)
    state_machines: list[str] = field(default_factory=list)
    known_risks: list[str] = field(default_factory=list)
    # 设计 10 改动 4.1:跟可执行测试的绑定(journey 执行状态回写,供 stale 检测 + 规划注入)
    test_ref: str = ""          # 指向 hc-pytest journey YAML 路径
    last_run_at: str = ""       # 最近一次 journey 执行时间
    last_status: str = ""       # PASS / FAIL / STALE
    verified_at: str = ""       # 人工确认时间

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "participating_services": self.participating_services,
                "main_flow": self.main_flow,
                "apis": self.apis,
                "tables": self.tables,
                "mq_topics": self.mq_topics,
                "state_machines": self.state_machines,
                "known_risks": self.known_risks,
                "test_ref": self.test_ref,
                "last_run_at": self.last_run_at,
                "last_status": self.last_status,
                "verified_at": self.verified_at,
            }
        )
        return data


@dataclass
class FileRef:
    """Reference to a file mentioned in a playbook."""

    path: str
    role: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "role": self.role, "count": self.count}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileRef":
        return cls(
            path=data["path"],
            role=data.get("role", ""),
            count=data.get("count", 0),
        )


@dataclass
class CommandRef:
    """Reference to a common command."""

    cls: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.cls, "count": self.count, "examples": self.examples}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommandRef":
        return cls(
            cls=data["class"],
            count=data.get("count", 0),
            examples=data.get("examples", []),
        )


@dataclass
class FailureRef:
    """Reference to a common failure."""

    category: str
    count: int = 0
    sample_text: str = ""
    mitigation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "count": self.count,
            "sample_text": self.sample_text,
            "mitigation": self.mitigation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureRef":
        return cls(
            category=data["category"],
            count=data.get("count", 0),
            sample_text=data.get("sample_text", ""),
            mitigation=data.get("mitigation", ""),
        )


@dataclass
class PlaybookEntry(KnowledgeEntry):
    """Dynamic task-experience knowledge."""

    theme: str = ""
    session_count: int = 0
    top_files: list[FileRef] = field(default_factory=list)
    common_commands: list[CommandRef] = field(default_factory=list)
    common_failures: list[FailureRef] = field(default_factory=list)
    linked_scenarios: list[str] = field(default_factory=list)
    linked_story: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "theme": self.theme,
                "session_count": self.session_count,
                "top_files": [f.to_dict() for f in self.top_files],
                "common_commands": [c.to_dict() for c in self.common_commands],
                "common_failures": [f.to_dict() for f in self.common_failures],
                "linked_scenarios": self.linked_scenarios,
                "linked_story": self.linked_story,
            }
        )
        return data


@dataclass
class FailureEntry(KnowledgeEntry):
    """Unified failure knowledge."""

    category: str = ""
    display_category: str = ""
    detail: str = ""
    frequency: dict[str, int] = field(default_factory=dict)
    common_tools: list[str] = field(default_factory=list)
    stages_affected: list[str] = field(default_factory=list)
    mitigations: list[str] = field(default_factory=list)
    counterfactuals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "category": self.category,
                "display_category": self.display_category,
                "detail": self.detail,
                "frequency": self.frequency,
                "common_tools": self.common_tools,
                "stages_affected": self.stages_affected,
                "mitigations": self.mitigations,
                "counterfactuals": self.counterfactuals,
            }
        )
        return data


@dataclass
class WikiEntry(KnowledgeEntry):
    """业务项目 wiki 页(11-workspace-entity-design.md §4)。

    type: wiki —— 知识层唯一"人优先"的条目类型:人读全文,agent 只读
    ``summary`` + ``related`` 指针(双读者,§4.2)。来源与生效规则(§4.3):

    - ``source: human`` → 直接生效(review_state=merged),不进 draft 管线
    - ``source: story:<key>`` / ``probe:<name>`` → 必须 draft → review →
      人工确认 → merge(I2:AI 不自动覆盖正式知识)

    review_state: draft | merged(暂不引入独立 review 态,reject 回 draft + reason)。
    evidence_refs: 论断的证据链 [{probe, query, observed_at}](§5.3),AI/probe 产出必填。
    probe_snapshot: probe 产出时的聚合快照,stale 检测重跑 probe 对比用(§5.3)。
    """

    summary: str = ""  # ≤200 字,agent 注入只用这段(§4.2)
    review_state: str = "draft"  # draft | merged(人写直接 merged)
    evidence_refs: list[dict] = field(default_factory=list)
    related: list[str] = field(default_factory=list)  # [scenario:xxx, playbook:yyy]
    verified_at: str = ""  # 人工确认时间
    reviewed_by: str = ""
    review_reason: str = ""
    probe_snapshot: dict = field(default_factory=dict)  # 聚合统计,无原始行(I5)
    content: str = ""  # 正文(给人看的完整叙述)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "summary": self.summary,
                "review_state": self.review_state,
                "evidence_refs": self.evidence_refs,
                "related": self.related,
                "verified_at": self.verified_at,
                "reviewed_by": self.reviewed_by,
                "review_reason": self.review_reason,
                "probe_snapshot": self.probe_snapshot,
                "content": self.content,
            }
        )
        return data
