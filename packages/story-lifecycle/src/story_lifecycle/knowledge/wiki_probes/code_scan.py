"""L1 代码扫描 probe(11-workspace-entity-design.md §5.1/§5.4)。

静态扫 API 注解/表定义/MQ 监听/依赖文件,产出**聚合统计**(I5 PII 红线:
计数/分布/名称清单,绝无原始用户数据行)。开源核心只带这一层;
DMS/SLS/ES/Mongo 等 L2-L4 probe 在 hc 侧仓库,不进本仓库。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .base import BaseWikiProbe, Evidence

_API_MAPPING_RE = re.compile(
    r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*[\"']([^\"']*)[\"']"
)
_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\"\[]?[\w.]+)", re.I
)
_JPA_TABLE_RE = re.compile(r"@Table\s*\(\s*name\s*=\s*[\"']([^\"']+)[\"']")
_MQ_RE = re.compile(
    r"@(?:RabbitListener|KafkaListener)\s*\(\s*(?:queues|topics)\s*=\s*[\"']([^\"']+)[\"']"
)

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "target",
    "build",
    "dist",
    ".venv",
    "venv",
    ".idea",
    ".gradle",
    "__pycache__",
}
_MAX_NAME_LIST = 20  # 名称清单封顶,保持条目有界


def _iter_files(root: Path):
    """遍历仓库源文件,跳过依赖/构建目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            yield Path(dirpath) / f


def _scan_java_apis(repo: Path) -> dict:
    """统计 API 注解:按 HTTP method 计数 + 去重路径清单 + 方法-路径冲突检测。"""
    method_counts: dict[str, int] = {}
    paths: dict[str, set[str]] = {}
    for file in _iter_files(repo):
        if file.suffix != ".java":
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _API_MAPPING_RE.finditer(text):
            path = m.group(1)
            method = "REQUEST" if "Request" in m.group(0) else _method_of(m.group(0))
            method_counts[method] = method_counts.get(method, 0) + 1
            paths.setdefault(path, set()).add(method)
    # 分歧:同一路径被多个不同 HTTP method 声明(代码内声明不一致)
    divergent = {p for p, ms in paths.items() if len(ms) > 1}
    return {
        "api_count": sum(method_counts.values()),
        "methods": dict(sorted(method_counts.items())),
        "top_paths": sorted(paths.keys())[:_MAX_NAME_LIST],
        "divergent_paths": sorted(divergent),
    }


def _method_of(annotation_text: str) -> str:
    for prefix in ("Get", "Post", "Put", "Delete", "Patch"):
        if f"@{prefix}Mapping" in annotation_text:
            return prefix.upper()
    return "REQUEST"


def _scan_tables(repo: Path) -> dict:
    """统计表定义:DDL CREATE TABLE + JPA @Table。"""
    names: set[str] = set()
    for file in _iter_files(repo):
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if file.suffix == ".sql":
            for m in _TABLE_RE.finditer(text):
                names.add(m.group(1).strip('`"[]'))
        elif file.suffix == ".java":
            for m in _JPA_TABLE_RE.finditer(text):
                names.add(m.group(1))
    return {"table_count": len(names), "tables": sorted(names)[:_MAX_NAME_LIST]}


def _scan_mq(repo: Path) -> dict:
    """统计 MQ 监听 topic/queue。"""
    topics: set[str] = set()
    for file in _iter_files(repo):
        if file.suffix != ".java":
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _MQ_RE.finditer(text):
            topics.add(m.group(1))
    return {"mq_topic_count": len(topics), "mq_topics": sorted(topics)[:_MAX_NAME_LIST]}


def _scan_dependencies(repo: Path) -> dict:
    """统计依赖文件:按构建系统类型计数 + 关键文件清单。"""
    kinds: list[str] = []
    for marker, kind in (
        ("pom.xml", "maven"),
        ("build.gradle", "gradle"),
        ("package.json", "node"),
        ("pyproject.toml", "python"),
        ("go.mod", "go"),
    ):
        if (repo / marker).exists():
            kinds.append(kind)
    return {"build_systems": kinds, "dependency_file_count": len(kinds)}


class CodeScanProbe(BaseWikiProbe):
    """L1 代码扫描:API 注解/表定义/MQ 监听/依赖文件(纯静态,无运行时依赖)。"""

    def probe(self, workspace: dict) -> list[Evidence]:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        evidence: list[Evidence] = []
        for repo in workspace.get("repos") or []:
            repo_path = (repo.get("repo_path") or "").strip()
            if not repo_path:
                continue
            repo_dir = Path(repo_path)
            if not repo_dir.is_dir():
                continue
            query = f"静态扫描 {repo_dir}"
            try:
                apis = _scan_java_apis(repo_dir)
                evidence.append(
                    Evidence(
                        layer="L1",
                        kind="api_endpoints",
                        summary=(
                            f"{repo.get('name', repo_dir.name)} 代码声明 "
                            f"{apis['api_count']} 个 API 端点"
                        ),
                        data=apis,
                        query=query,
                        observed_at=now,
                    )
                )
                if apis["divergent_paths"]:
                    evidence.append(
                        Evidence(
                            layer="L1",
                            kind="api_divergence",
                            summary=(
                                f"{repo.get('name', repo_dir.name)} 存在代码内 API 声明分歧:"
                                f"同一路径被多个 HTTP method 声明,以实际实现为准"
                            ),
                            data={"divergent_paths": apis["divergent_paths"]},
                            query=query,
                            observed_at=now,
                        )
                    )
                tables = _scan_tables(repo_dir)
                if tables["table_count"]:
                    evidence.append(
                        Evidence(
                            layer="L1",
                            kind="table_definitions",
                            summary=(
                                f"{repo.get('name', repo_dir.name)} 代码定义 "
                                f"{tables['table_count']} 张表"
                            ),
                            data=tables,
                            query=query,
                            observed_at=now,
                        )
                    )
                mq = _scan_mq(repo_dir)
                if mq["mq_topic_count"]:
                    evidence.append(
                        Evidence(
                            layer="L1",
                            kind="mq_topics",
                            summary=(
                                f"{repo.get('name', repo_dir.name)} 监听 "
                                f"{mq['mq_topic_count']} 个 MQ topic/queue"
                            ),
                            data=mq,
                            query=query,
                            observed_at=now,
                        )
                    )
                deps = _scan_dependencies(repo_dir)
                if deps["build_systems"]:
                    evidence.append(
                        Evidence(
                            layer="L1",
                            kind="dependency_files",
                            summary=(
                                f"{repo.get('name', repo_dir.name)} 构建系统: "
                                f"{', '.join(deps['build_systems'])}"
                            ),
                            data=deps,
                            query=query,
                            observed_at=now,
                        )
                    )
            except Exception:  # noqa: BLE001 — 单仓库扫描失败不阻断(§5.2 容错)
                continue
        return evidence
