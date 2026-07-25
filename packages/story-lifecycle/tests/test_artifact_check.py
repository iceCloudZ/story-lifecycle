"""1.2 — check_artifacts_landed 纯函数测试。

设计依据:DESIGN §1.2。成果物落地是 stage 完成的地面真相。三种形式:
普通路径(存在+非空)/ glob(任一存在+非空)/ "git"(status 非空)。空文件不算落地。
"""

from __future__ import annotations

import subprocess

from story_lifecycle.orchestrator.engine.artifact_check import (
    artifacts_ready,
    check_artifacts_landed,
)


# ---- 文件路径形式 ----


def test_missing_file_is_missing(tmp_path):
    missing, landed = check_artifacts_landed(["story/spec.md"], str(tmp_path))
    assert missing == ["story/spec.md"]
    assert landed == []


def test_existing_nonempty_file_landed(tmp_path):
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "spec.md").write_text("# 设计方案\n", encoding="utf-8")
    missing, landed = check_artifacts_landed(["story/spec.md"], str(tmp_path))
    assert missing == []
    assert landed == ["story/spec.md"]


def test_empty_file_is_missing(tmp_path):
    """空文件不算落地 —— 防止 code agent touch 空文件骗过检查。"""
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "spec.md").write_text("", encoding="utf-8")
    missing, landed = check_artifacts_landed(["story/spec.md"], str(tmp_path))
    assert missing == ["story/spec.md"]
    assert landed == []


# ---- glob 形式 ----


def test_glob_any_match_landed(tmp_path):
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "test-report.md").write_text("测过了\n", encoding="utf-8")
    missing, landed = check_artifacts_landed(["story/*.md"], str(tmp_path))
    assert missing == []
    assert landed == ["story/*.md"]


def test_glob_no_match_is_missing(tmp_path):
    (tmp_path / "story").mkdir()
    missing, landed = check_artifacts_landed(["story/*.md"], str(tmp_path))
    assert missing == ["story/*.md"]
    assert landed == []


def test_glob_ignores_empty_match(tmp_path):
    """glob 命中但文件空 → 仍 missing(空不算落地)。"""
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "empty.md").write_text("", encoding="utf-8")
    missing, landed = check_artifacts_landed(["story/*.md"], str(tmp_path))
    assert missing == ["story/*.md"]
    assert landed == []


# ---- git 形式 ----


def test_git_no_changes_is_missing(tmp_path):
    """git 仓库但无改动 → missing(代码类 stage 必须真改了代码)。

    用 tmp_path 的子目录当仓库根,隔绝 pytest 临时区可能残留的未跟踪文件
    (其他测试在 pytest tmp 父目录留下的 story-home/ 等会污染 git status)。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=str(repo), capture_output=True
    )
    # commit 一个占位文件让仓库有 HEAD(否则 git status 行为在空仓库上不稳)。
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
    )
    missing, landed = check_artifacts_landed(["git"], str(repo))
    assert missing == ["git"]
    assert landed == []


def test_git_with_changes_landed(tmp_path):
    """git 有改动(未提交)→ landed。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=str(repo), capture_output=True
    )
    # 先 commit 一个基线文件,让仓库有 HEAD。
    (repo / "baseline.py").write_text("# base\n", encoding="utf-8")
    subprocess.run(["git", "add", "baseline.py"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
    )
    # 再加一个未提交改动 → status 非空。
    (repo / "code.py").write_text("print('hi')\n", encoding="utf-8")
    missing, landed = check_artifacts_landed(["git"], str(repo))
    assert missing == []
    assert landed == ["git"]


def test_git_not_a_repo_is_missing(tmp_path, monkeypatch):
    """非 git 仓库 → git 调用失败 → 安全视为 missing(不抛)。"""
    monkeypatch.chdir(tmp_path)  # 确保 tmp_path 不是 git 仓库
    missing, landed = check_artifacts_landed(["git"], str(tmp_path))
    assert missing == ["git"]
    assert landed == []


# ---- 组合 + 边界 ----


def test_mixed_all_landed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "story").mkdir()
    (repo / "story" / "spec.md").write_text("设计\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=str(repo), capture_output=True
    )
    # commit 基线 → 有 HEAD,后续未提交改动才算 changes。
    (repo / "baseline.py").write_text("# base\n", encoding="utf-8")
    subprocess.run(["git", "add", "baseline.py"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
    )
    (repo / "main.py").write_text("x=1\n", encoding="utf-8")
    missing, landed = check_artifacts_landed(
        ["story/spec.md", "git"], str(repo)
    )
    assert missing == []
    assert set(landed) == {"story/spec.md", "git"}


def test_partial_landing_reports_only_missing(tmp_path):
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "spec.md").write_text("设计\n", encoding="utf-8")
    # test-report.md 不存在
    missing, landed = check_artifacts_landed(
        ["story/spec.md", "story/test-report.md"], str(tmp_path)
    )
    assert missing == ["story/test-report.md"]
    assert landed == ["story/spec.md"]


def test_empty_artifacts_returns_empty(tmp_path):
    """空 artifacts 输入 → ([],[])(1.1 schema 契约应已拦下,这里防御)。"""
    missing, landed = check_artifacts_landed([], str(tmp_path))
    assert missing == []
    assert landed == []


def test_artifacts_ready_predicate(tmp_path):
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "spec.md").write_text("设计\n", encoding="utf-8")
    assert artifacts_ready(["story/spec.md"], str(tmp_path)) is True
    assert artifacts_ready(["story/missing.md"], str(tmp_path)) is False


def test_invalid_entries_treated_as_missing(tmp_path):
    """非字符串 / 空串 → missing(防御脏 profile 数据)。"""
    missing, landed = check_artifacts_landed(
        ["", "story/spec.md"], str(tmp_path)
    )
    assert "" in missing
    assert "story/spec.md" in missing  # spec.md 没建


# ---- evidence_candidates 兜底(STEP 1.4 强化) ----


def test_evidence_candidate_fallback_when_workspace_path_missing(tmp_path):
    """workspace 相对路径没落地,但 evidence 候选有 → landed(robust 兜底)。

    场景:code agent 把 spec 写到 story evidence 目录(story_evidence_root 向上找
    AGENTS.md 脱离 workspace),而非 workspace/story/spec.md。
    """
    ev_path = tmp_path / "evidence" / "spec.md"
    ev_path.parent.mkdir(parents=True)
    ev_path.write_text("设计\n", encoding="utf-8")
    # workspace/story/spec.md 不存在
    cands = {"story/spec.md": [str(ev_path)]}
    missing, landed = check_artifacts_landed(
        ["story/spec.md"], str(tmp_path), evidence_candidates=cands
    )
    assert missing == []
    assert landed == ["story/spec.md"]


def test_evidence_candidate_alias_filename_design_md(tmp_path):
    """code agent 用别名 design.md 而非 spec.md → evidence 候选含别名也能命中。"""
    ev_design = tmp_path / "evidence" / "design.md"
    ev_design.parent.mkdir(parents=True)
    ev_design.write_text("设计\n", encoding="utf-8")
    cands = {"story/spec.md": [str(tmp_path / "evidence" / "spec.md"), str(ev_design)]}
    missing, landed = check_artifacts_landed(
        ["story/spec.md"], str(tmp_path), evidence_candidates=cands
    )
    assert missing == []


def test_build_evidence_candidates_returns_canonical_paths(tmp_path):
    """build_evidence_candidates 为 spec artifact 返回 evidence_dir 下 spec.md + design.md。"""
    from story_lifecycle.orchestrator.engine.artifact_check import (
        build_evidence_candidates,
    )

    cands = build_evidence_candidates(
        ["story/spec.md", "git"], str(tmp_path), "K1", title="t"
    )
    assert "story/spec.md" in cands
    # spec → spec.md + design.md 别名
    paths = cands["story/spec.md"]
    assert any(p.endswith("spec.md") for p in paths)
    assert any(p.endswith("design.md") for p in paths)
    # git artifact 不需要候选
    assert "git" not in cands


def test_evidence_candidate_empty_when_artifact_not_in_map(tmp_path):
    """未知 artifact 路径(不在 _ARTIFACT_TO_DOC_TYPE)→ 无候选(只查 workspace 相对)。"""
    from story_lifecycle.orchestrator.engine.artifact_check import (
        build_evidence_candidates,
    )

    cands = build_evidence_candidates(["custom/file.md"], str(tmp_path), "K1")
    # 未知映射 → 不在候选里(防御)
    assert "custom/file.md" not in cands
