# /release — bump version, update CHANGELOG, commit & tag

## 参数

用户可指定版本号或 bump 类型：
- `/release` — patch bump（如 `0.5.10` → `0.5.11`）
- `/release 0.6.0` — 指定版本号
- `/release minor` — minor bump（如 `0.5.11` → `0.6.0`）
- `/release major` — major bump（如 `0.5.11` → `1.0.0`）

## 流程

1. **确认版本号**：根据参数或当前版本 bump。读取 `pyproject.toml` 中的 `version`。
2. **收集变更**：
   - 如果 HEAD 有未提交的 diff，将其作为本次发布内容（分析 diff 归类）。
   - 否则运行 `git log <last-tag>..HEAD --oneline`，按 commit message 归类。
3. **分类变更**：将变更按 `Added` / `Changed` / `Fixed` / `Removed` 分类，每条用中文概括，与 CHANGELOG 已有条目风格一致。
4. **更新 CHANGELOG.md**：在 `# Changelog` 标题后插入新版本段落：
   ```
   ## [X.Y.Z] - YYYY-MM-DD

   ### Fixed
   - 条目
   ```
   无条目的分类省略。日期用当天。
5. **更新 pyproject.toml**：`version = "X.Y.Z"`。
6. **提交**：
   - 如果有未提交的代码变更，全部 `git add` 后一起提交。
   - 否则只 `git add CHANGELOG.md pyproject.toml`。
   - commit message: `release: vX.Y.Z`（带 Co-Authored-By）。
7. **打 tag**：`git tag vX.Y.Z`。
8. **推送**：`git push && git push origin vX.Y.Z`，触发远程 CI/CD。
9. **观察 CI**：推送后用 `gh run list --limit 5` 找到本次 release workflow，轮询 `gh run watch <run_id>` 等待结果：
   - **成功**：确认 PyPI 版本已更新（`pip index versions story-lifecycle`），告知用户发布完成。
   - **失败**：用 `gh run view <run_id> --log-failed` 查看失败原因，报告给用户并提示修复方案。

## 注意

- 自动 push commit + tag，触发远程 pipeline。
- 未提交的代码变更默认一起发布，无需额外确认（用户主动调用 `/release` 即为确认）。
- 推送后必须观察 CI 结果，确认发布成功再结束。
