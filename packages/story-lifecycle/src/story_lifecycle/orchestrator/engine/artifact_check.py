"""check_artifacts_landed — 成果物落地判定(纯函数,零 LLM,零 DB 副作用)。

DESIGN-artifact-driven-stage-completion §1.2 / STEP 1 子任务 1.2。

成果物是 stage 完成的地面真相(替不可信的 done.json 自报)。本模块确定性检查
profile.stage.artifacts 声明的文件/路径/glob/\"git\" 标记是否落地。

artifacts 三种形式(都在 StageConfig.artifacts 注释里定义):
  - 普通文件路径(workspace 相对,如 "story/spec.md"):Path 存在且非空 → landed
  - glob(workspace 相对,含 * ? [):任一 match 存在且非空 → landed
  - 字面量 "git":"git -C <workspace> status --porcelain" 输出非空 → landed

返回 (missing, landed):全 landed = stage 完成信号达成;missing 列出缺哪些。
纯函数:无 DB / 无 LLM / 无文件写入;读 + 子进程查。失败安全(git 不可用 → 视为
missing,不抛 —— 编排器据此决定等待还是 escalate,不让一个 git 调用炸掉推进)。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("story-lifecycle.artifact_check")

# git status 子进程超时(秒)。工作区可能很大(.git 慢),给宽松上限但必须有界,
# 不让一个卡住的 git 把 poll loop 拖死。超时按 missing 处理(编排器继续等 / escalate)。
_GIT_STATUS_TIMEOUT = 15

# glob 元字符(用于区分"普通路径" vs "glob 模式")
_GLOB_CHARS = set("*?[]")


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in _GLOB_CHARS)


def _file_landed(path: Path) -> bool:
    """文件存在且非空 → landed。空文件视为未落地(避免 code agent touch 空文件骗过)。"""
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _glob_landed(workspace: Path, pattern: str) -> bool:
    """glob 任一 match 存在且非空 → landed。"""
    try:
        for match in workspace.glob(pattern):
            if match.is_file() and match.stat().st_size > 0:
                return True
    except (OSError, ValueError):
        pass
    return False


def _git_has_changes(workspace: Path) -> bool:
    """git status --porcelain 输出非空 → 有改动 → landed。

    失败安全:git 不可用 / 非仓库 / 超时 → False(视为 missing)。编排器据此决策,
    不让 git 调用异常炸掉推进逻辑。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_GIT_STATUS_TIMEOUT,
        )
        return bool(result.stdout.strip())
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
        OSError,
    ) as exc:
        log.debug(
            "git status check failed (treating as not-landed): workspace=%s %s",
            workspace,
            exc,
        )
        return False


def check_artifacts_landed(
    artifacts: list[str],
    workspace: str,
    *,
    evidence_candidates: dict[str, list[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """检查一组 artifacts 是否落地。

    Args:
        artifacts: stage 声明的成果物标记(文件路径/glob/"git"),workspace 相对。
        workspace: 工作区根(absolute path string)。
        evidence_candidates: 可选 — {artifact: [额外查找的绝对路径列表]}。code agent
            可能把成果物写到 story evidence 目录(story_doc_path 解析的 canonical 位置,
            因 story_evidence_root 会向上找 AGENTS.md 而脱离 workspace)。这里允许为
            每个 artifact 列出额外的绝对路径候选,任一存在+非空就算落地(robust 兜底,
            设计 §7.6 "code agent 不调 story-tool → 降级自己写文件")。

    Returns:
        (missing, landed) —— 两个 list,元素是 artifact 字符串本身。
        全 landed(missing 为空)= stage 完成信号达成。

    纯函数:只读 + 子进程查,无副作用。空 artifacts 输入 → ([],[])(调用方应已
    被 1.1 schema 契约拦下,这里防御性返回不抛)。
    """
    if not artifacts:
        return [], []

    ws = Path(workspace)
    extra = evidence_candidates or {}
    missing: list[str] = []
    landed: list[str] = []

    for artifact in artifacts:
        if not isinstance(artifact, str) or not artifact:
            missing.append(str(artifact))
            continue

        if artifact == "git":
            ok = _git_has_changes(ws)
        elif _is_glob(artifact):
            ok = _glob_landed(ws, artifact)
        else:
            ok = _file_landed(ws / artifact)
            # 兜底:workspace 相对路径没落地 → 查 evidence 候选(code agent 可能写到
            # story evidence 目录或用别的文件名如 design.md)。
            if not ok:
                for cand in extra.get(artifact, []):
                    if _file_landed(Path(cand)):
                        ok = True
                        break

        if ok:
            landed.append(artifact)
        else:
            missing.append(artifact)

    return missing, landed


def artifacts_ready(
    artifacts: list[str], workspace: str, *, evidence_candidates=None
) -> bool:
    """便捷谓词:成果物全齐(missing 为空)→ True。"""
    missing, _ = check_artifacts_landed(
        artifacts, workspace, evidence_candidates=evidence_candidates
    )
    return not missing


# artifact 路径 → doc_type 映射(用于 evidence_candidates 查找)。
# "story/spec.md" → "spec"; "story/test-report.md" → "test_report"。
_ARTIFACT_TO_DOC_TYPE = {
    "story/spec.md": "spec",
    "story/test-report.md": "test_report",
    "story/research.md": "research",
    "story/plan.md": "plan",
    "story/delivery.md": "delivery",
    "story/review-verdict.md": "review_verdict",
}

# doc_type 可能的 evidence 文件名(code agent 常见别名,robust 兜底):
#   spec → spec.md / design.md(code agent 爱用 design 命名)
#   test_report → test-report.md / test_report.md
_DOC_TYPE_FILENAMES = {
    "spec": ["spec.md", "design.md"],
    "test_report": ["test-report.md", "test_report.md", "testreport.md"],
    "research": ["research.md"],
    "plan": ["plan.md"],
    "delivery": ["delivery.md"],
    "review_verdict": ["review-verdict.md", "review_verdict.md"],
}


def build_evidence_candidates(
    artifacts: list[str], workspace: str, story_key: str, title: str = ""
) -> dict[str, list[str]]:
    """为每个 artifact 构建 evidence-dir 候选绝对路径列表(robust 兜底)。

    code agent 可能直接写 story evidence 目录(story_doc_path 解析的 canonical 位置,
    因 story_evidence_root 向上找 AGENTS.md 而脱离 workspace),也可能用别名文件名
    (design.md 而非 spec.md)。本函数为每个文件类 artifact 列出 evidence 候选,
    让 check_artifacts_landed 在 workspace 相对路径没落地时能兜底命中。

    返回 {artifact: [abs_path, ...]}。git / glob 类 artifact 不需要(返回空列表)。
    """
    from ...infra.story_paths import story_evidence_dir

    try:
        evidence_dir = story_evidence_dir(workspace, story_key, title)
    except Exception:  # noqa: BLE001
        return {}

    candidates: dict[str, list[str]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, str) or artifact == "git" or _is_glob(artifact):
            continue
        doc_type = _ARTIFACT_TO_DOC_TYPE.get(artifact)
        if not doc_type:
            continue
        names = _DOC_TYPE_FILENAMES.get(doc_type, [])
        candidates[artifact] = [str(evidence_dir / name) for name in names]
    return candidates
