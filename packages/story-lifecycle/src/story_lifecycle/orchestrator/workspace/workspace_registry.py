"""Workspace entity registry & init pipeline (11-workspace-entity-design.md).

Workspace = 业务项目实体(新顶层),聚合 Repo(project 表)、wiki 知识、旅程、集成。
显式 opt-in —— 不建 Workspace 时行为与今天完全一致。

Init pipeline(§3): 声明式 step 序列,顺序执行、每步幂等、失败不阻塞后续、
可单步重跑(`--step`)。状态存 workspace.init_state。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ...infra.db import models as db
from .project_registry import (
    check_project_availability,
    register_project,
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(name: str) -> str:
    """kebab-case 化:小写、非字母数字 → '-',压缩连字符。"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug or ""):
        raise ValueError(
            f"Invalid slug: {slug!r} — 必须为 kebab-case(小写字母/数字/连字符)"
        )


def create_workspace(
    name: str,
    slug: str | None = None,
    knowledge_root: str | None = None,
) -> dict:
    """Create a workspace entity. name 必填;slug 缺省由 name 派生。"""
    if not name or not name.strip():
        raise ValueError("workspace name must not be empty")
    slug = slug or slugify(name)
    validate_slug(slug)
    if knowledge_root:
        knowledge_root = str(Path(knowledge_root).resolve())
        Path(knowledge_root).mkdir(parents=True, exist_ok=True)
    return db.create_workspace(name.strip(), slug, knowledge_root=knowledge_root)


def get_workspace(ident: int | str) -> dict | None:
    """Resolve a workspace by id(int)/slug/name(str)。"""
    if isinstance(ident, int):
        return db.get_workspace(ident)
    if isinstance(ident, str) and ident.isdigit():
        return db.get_workspace(int(ident))
    return db.get_workspace_by_slug(ident) or db.get_workspace_by_name(ident)


def list_workspaces() -> list[dict]:
    return db.list_workspaces()


def delete_workspace(ident: int | str) -> bool:
    ws = get_workspace(ident)
    if not ws:
        return False
    db.delete_workspace(ws["id"])
    return True


# -------- Init pipeline (§3) --------


def _infer_workspace_root(repos: list[dict]) -> Path | None:
    """从 repo 路径推断 主工作区 根目录(找带 .story/.agents/AGENTS.md 标记的祖先)。

    同 api.py 的 _workspace_root_for_project 口径:单仓库 = 仓库本身;
    monorepo = 携带标记的公共祖先。找不到标记就取 git top-level。
    """
    roots: list[Path] = []
    for repo in repos:
        path = Path(repo.get("repo_path") or "").resolve()
        if not path.exists():
            continue
        git_root: Path | None = None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                git_root = Path(result.stdout.strip()).resolve()
        except Exception:
            pass
        max_depth = 5
        candidates = [path]
        for i, parent in enumerate(path.parents):
            if git_root is not None and parent == git_root:
                candidates.append(parent)
                break
            if i >= max_depth:
                break
            candidates.append(parent)
        picked = None
        for candidate in candidates:
            if (
                (candidate / ".story").exists()
                or (candidate / ".agents").exists()
                or (candidate / "AGENTS.md").exists()
            ):
                picked = candidate
                break
        roots.append(picked or git_root or path)
    if not roots:
        return None
    # 公共祖先 = 所有根共有的最长前缀目录(带标记者优先)
    root = Path(roots[0])
    for other in roots[1:]:
        root = _common_parent(root, other)
    return root


def _common_parent(a: Path, b: Path) -> Path:
    try:
        a_parts = a.resolve().parts
        b_parts = b.resolve().parts
    except Exception:
        return a
    common: list[str] = []
    for pa, pb in zip(a_parts, b_parts):
        if pa != pb:
            break
        common.append(pa)
    if not common:
        return a
    return Path(*common)


def _detect_runtime_facts(project: dict) -> list[dict]:
    """轻量运行时探测:按仓库根目录标志文件推断运行时类型(现有 project_runtime_fact 表)。

    返回 upsert 后的 fact 列表。探测不到不报错 —— 该层事实缺失即优雅降级。
    """
    repo_path = Path(project.get("repo_path") or "")
    if not repo_path.exists():
        return []
    markers = {
        "pom.xml": ("maven", "mvn -v"),
        "build.gradle": ("gradle", "gradle --version"),
        "package.json": ("node", "node --version"),
        "pyproject.toml": ("python", "python --version"),
        "go.mod": ("go", "go version"),
        "Cargo.toml": ("rust", "cargo --version"),
    }
    facts: list[dict] = []
    for filename, (runtime_type, check_command) in markers.items():
        if (repo_path / filename).exists():
            facts.append(
                db.upsert_runtime_facts(
                    project_id=project["id"],
                    runtime_type=runtime_type,
                    check_command=check_command,
                    availability="unknown",
                )
            )
    return facts


def _step_register_repos(ws: dict, repos: list[tuple[str, str]]) -> dict:
    """step 1: 注册 Repo(repo_path/默认分支)→ project 表加 workspace_id。幂等。"""
    added: list[str] = []
    if repos:
        for name, path in repos:
            proj = register_project(name=name, repo_path=path)
            db.update_project(proj["id"], workspace_id=ws["id"])
            added.append(proj["name"])
    existing = db.list_projects_by_workspace(ws["id"])
    if not added and not existing:
        return {
            "status": "failed",
            "reason": "Workspace 下没有任何 Repo,请提供 --repo name=path 至少一个",
        }
    detail = (
        f"Repos: {', '.join(added) or '已有 ' + ', '.join(p['name'] for p in existing)}"
    )
    return {"status": "done", "detail": detail}


def _step_detect_runtime(ws: dict) -> dict:
    """step 2: 探测运行时事实 → project_runtime_fact(现有机制)。自动跑。"""
    projects = db.list_projects_by_workspace(ws["id"])
    if not projects:
        return {
            "status": "failed",
            "reason": "没有 Repo 可探测,先跑 register_repos",
        }
    for p in projects:
        check_project_availability(p["id"])
        _detect_runtime_facts(p)
    return {
        "status": "done",
        "detail": f"探测 {len(projects)} 个仓库的运行时事实",
    }


def _knowledge_root_for(ws: dict) -> str | None:
    """解析 knowledge_root:显式配置 → 直接用;否则从 repos 推断主工作区根。"""
    if ws.get("knowledge_root"):
        return ws["knowledge_root"]
    repos = db.list_projects_by_workspace(ws["id"])
    root = _infer_workspace_root(repos)
    if root is None:
        return None
    return str(root / ".story" / "knowledge")


def _step_gen_wiki(ws: dict) -> dict:
    """step 3: gen_wiki —— L1 代码扫描生成 wiki 骨架 + probe 增补,全部 draft(§4/§5)。

    Phase 3:跑配置的 wiki_probes(缺省核心自带 CodeScanProbe L1)把证据落成
    draft wiki 页(带 evidence_refs 证据链)。L2-L4 专用 probe 在 hc 侧仓库,
    没配就缺哪层,优雅降级。probe 产出永远 draft,生效走人工确认(I2)。
    """
    kroot = _knowledge_root_for(ws)
    if kroot is None:
        return {
            "status": "failed",
            "reason": "无法确定知识根目录:没有 Repo 也没有显式 knowledge_root",
        }
    db.update_workspace(ws["id"], knowledge_root=kroot)
    wiki_dir = Path(kroot) / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    placeholder = wiki_dir / "README.md"
    if not placeholder.exists():
        placeholder.write_text(
            "## Wiki\n\n"
            "本目录是 Workspace 的业务 wiki 知识(§4)。"
            "probe/story 产出为 draft,人工确认(merge)后生效;"
            "人写条目(source: human)直接生效。\n",
            encoding="utf-8",
        )
    try:
        from ...knowledge.wiki_pipeline import generate_wiki_drafts

        ws_dict = {
            "id": ws["id"],
            "name": ws["name"],
            "knowledge_root": kroot,
            "repos": db.list_projects_by_workspace(ws["id"]),
        }
        drafts = generate_wiki_drafts(ws_dict)
        return {
            "status": "done",
            "detail": f"wiki 骨架就位 + {len(drafts)} 条 probe draft(待人确认)",
        }
    except Exception as e:  # noqa: BLE001 — probe 失败不阻塞管线,标记 failed + 原因
        return {"status": "failed", "reason": f"wiki 生成失败: {e}"}


def _step_register_integrations(ws: dict, integrations: dict | None) -> dict:
    """step 4: 登记 GitLab/CI/测试框架元数据(§6)。只登记展示,不校验连通性(D7)。"""
    merged = json.loads(ws.get("integrations_json") or "{}")
    if integrations:
        merged.update(integrations)
    db.update_workspace(
        ws["id"], integrations_json=json.dumps(merged, ensure_ascii=False)
    )
    if not merged:
        return {
            "status": "done",
            "detail": "无集成配置(可稍后 --integrations-json 补登)",
        }
    return {"status": "done", "detail": f"登记: {', '.join(merged.keys())}"}


def _step_init_scenarios(ws: dict) -> dict:
    """step 5: 跑现有 scenario 知识生成,旅程目录就位(08-init-knowledge 交互的自动化版)。"""
    kroot = _knowledge_root_for(ws)
    if kroot is None:
        return {
            "status": "failed",
            "reason": "无法确定知识根目录:没有 Repo 也没有显式 knowledge_root",
        }
    root = Path(kroot).parent
    try:
        from ...knowledge.knowledge_store.detector import detect_project
        from ...knowledge.knowledge_store.generator import generate_knowledge_files
        from ...knowledge.knowledge_store.scope import recommend_scope

        detection = detect_project(root)
        scope = recommend_scope(detection)
        created = generate_knowledge_files(root, detection, scope)
        db.update_workspace(ws["id"], knowledge_root=kroot)
        return {
            "status": "done",
            "detail": f"生成 {len(created)} 个知识文件,scenarios 目录就位",
        }
    except Exception as e:  # noqa: BLE001 — 知识层失败不阻塞管线,标记 failed + 原因
        return {"status": "failed", "reason": f"scenario 知识生成失败: {e}"}


def _step_detect_test_env(ws: dict) -> dict:
    """step 6: 扫描测试环境配置(gateway/MQ/DB/测试用户),写 integrations_json.test_env(draft)。

    扫描来源:
      - <workspace_root>/hc-pytest/conftest.yaml → gateways/mq_proxy/fixtures
      - 各 repo 的 bootstrap-local.yml → nacos addr / datasource url(提取 host,不提取密码)

    产出 _scan_status=draft,前端展示等人工确认。确认后 verify prompt 才注入。
    """
    import yaml

    repos = db.list_projects_by_workspace(ws["id"])
    ws_root = _infer_workspace_root(repos)
    if ws_root is None:
        return {"status": "skipped", "reason": "无法推断工作区根目录"}

    test_env: dict = {"_scan_status": "draft"}

    # 1. hc-pytest/conftest.yaml（最权威的测试环境配置源）
    conftest = ws_root / "hc-pytest" / "conftest.yaml"
    if conftest.exists():
        try:
            cfg = yaml.safe_load(conftest.read_text(encoding="utf-8")) or {}
            if cfg.get("env"):
                test_env["env"] = cfg["env"]
            if cfg.get("gateways"):
                test_env["gateways"] = cfg["gateways"]
            if cfg.get("mq_proxy"):
                test_env.setdefault("mq", {})["proxy"] = cfg["mq_proxy"]
            # fixtures
            fixtures = {}
            for key in (
                "test_mobile",
                "test_user_id",
                "test_verify_code",
                "test_device_id",
                "test_id_no",
            ):
                if key in cfg:
                    fixtures[key] = cfg[key]
            if fixtures:
                test_env["fixtures"] = fixtures
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "reason": f"conftest.yaml 解析失败: {e}"}

    # 2. 各 repo 的 bootstrap-local.yml（提取 nacos/datasource host）
    db_info: dict = {}
    nacos_addr = ""
    for repo in repos:
        repo_path = Path(repo.get("repo_path") or "")
        for yml_name in (
            "bootstrap-local.yml",
            "bootstrap-local.yaml",
        ):
            yml = repo_path / "src" / "main" / "resources" / yml_name
            if not yml.exists():
                yml = repo_path / yml_name
            if not yml.exists():
                continue
            try:
                content = yml.read_text(encoding="utf-8")
                # 轻量提取(不完整解析 YAML,只抓关键字段,避免多文档/占位符炸)
                for line in content.splitlines():
                    stripped = line.strip()
                    if "server-addr" in stripped and ":" in stripped:
                        # nacos: server-addr: xxx:8848
                        val = stripped.split(":", 1)[1].strip().strip("\"'")
                        if val and not nacos_addr:
                            nacos_addr = val
                    if "jdbc:" in stripped and "mysql" in stripped:
                        # datasource url 抓 host
                        import re

                        m = re.search(r"//([^/:]+)", stripped)
                        if m:
                            host = m.group(1)
                            db_info.setdefault("hosts", [])
                            if host not in db_info["hosts"]:
                                db_info["hosts"].append(host)
            except Exception:
                pass  # 单个 repo 解析失败不影响整体

    if nacos_addr:
        test_env.setdefault("config", {})["nacos"] = nacos_addr
    if db_info:
        test_env["database"] = db_info
        test_env["database"]["note"] = "凭据在各服务 bootstrap-local.yml"

    if not test_env or test_env == {"_scan_status": "draft"}:
        return {
            "status": "done",
            "detail": "未扫到测试环境配置(无 conftest.yaml / bootstrap-local.yml)",
        }

    # 写入 integrations_json.test_env
    merged = json.loads(ws.get("integrations_json") or "{}")
    merged["test_env"] = test_env
    db.update_workspace(ws["id"], integrations_json=json.dumps(merged, ensure_ascii=False))

    sources = []
    if conftest.exists():
        sources.append("conftest.yaml")
    if db_info or nacos_addr:
        sources.append("bootstrap-local.yml")
    return {
        "status": "done",
        "detail": f"扫描到测试环境配置({', '.join(sources)}),draft 待确认",
    }


def confirm_test_env(ident: int | str, test_env: dict) -> dict:
    """用户确认/编辑测试环境配置 → 标记 _scan_status=confirmed。"""
    ws = get_workspace(ident)
    if not ws:
        raise ValueError(f"Workspace not found: {ident}")
    test_env["_scan_status"] = "confirmed"
    merged = json.loads(ws.get("integrations_json") or "{}")
    merged["test_env"] = test_env
    db.update_workspace(ws["id"], integrations_json=json.dumps(merged, ensure_ascii=False))
    return merged["test_env"]


_STEP_RUNNERS = {
    "register_repos": _step_register_repos,
    "detect_runtime": _step_detect_runtime,
    "gen_wiki": _step_gen_wiki,
    "register_integrations": _step_register_integrations,
    "init_scenarios": _step_init_scenarios,
    "detect_test_env": _step_detect_test_env,
}


def run_init_pipeline(
    ident: int | str,
    *,
    step: str | None = None,
    repos: list[tuple[str, str]] | None = None,
    integrations_json: dict | None = None,
) -> list[dict]:
    """Run the init pipeline(或单步 --step)。顺序执行,每步失败不阻塞后续。

    Returns per-step results: [{step, status, reason, detail}, ...]。
    """
    ws = get_workspace(ident)
    if not ws:
        raise ValueError(f"Workspace not found: {ident}")
    steps = [step] if step else list(db.WORKSPACE_INIT_STEPS)
    for s in steps:
        if s not in db.WORKSPACE_INIT_STEPS:
            raise ValueError(
                f"Unknown init step: {s} — 可选: {', '.join(db.WORKSPACE_INIT_STEPS)}"
            )

    results: list[dict] = []
    for s in steps:
        runner = _STEP_RUNNERS[s]
        kwargs: dict = {}
        if s == "register_repos":
            kwargs["repos"] = repos or []
        elif s == "register_integrations":
            kwargs["integrations"] = integrations_json
        try:
            out = runner(ws, **kwargs)
            status = out.get("status", "failed")
            reason = out.get("reason", "")
            detail = out.get("detail", "")
        except Exception as e:  # noqa: BLE001 — 每步失败标记 failed + 原因,不中断管线
            status, reason, detail = "failed", str(e), ""
        db.update_workspace_init_state(ws["id"], s, status, reason)
        results.append(
            {"step": s, "status": status, "reason": reason, "detail": detail}
        )
    return results
