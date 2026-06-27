# M2 验证报告：子包 pyproject + import 调整

## 目标

- 两子包可独立 `pip install -e`
- import 路径可用
- 原测试全绿
- 绝对路径等基础设施路径适配 monorepo 布局

## 新增 / 修改文件

| 文件 | 改动 |
|------|------|
| `packages/story-miner/pyproject.toml` | 新增，包名 `story-miner`，模块 `miner`，Python ≥3.10，dev 依赖 pytest |
| `packages/story-lifecycle/src/story_lifecycle/cli/list_cmd.py` | 更新 miner retrospect 钩子路径，指向 `packages/story-miner/scripts/retrospect.py` |
| `packages/story-miner/scripts/*.py` | 替换 `D:/github/agent-transcript-miner` 为 `D:/github/story-lifecycle/packages/story-miner` |
| `packages/story-miner/CONTEXT.md` | 同上路经更新 |
| `packages/story-miner/docs/ROADMAP.md` | 同上路经更新 |
| `.gitignore` | 增加 `.venv-*/` 忽略测试虚拟环境 |

## 安装验证

命令：

```bash
python -m venv .venv-monorepo-test
.venv-monorepo-test/Scripts/python.exe -m pip install -e packages/story-miner
.venv-monorepo-test/Scripts/python.exe -m pip install -e packages/story-lifecycle
```

结果：
- `story-miner-0.1.0` 安装成功
- `story-lifecycle-0.11.6` 安装成功

导入验证：

```bash
.venv-monorepo-test/Scripts/python.exe -c "import miner; print(miner.__file__)"
.venv-monorepo-test/Scripts/python.exe -c "import story_lifecycle; print(story_lifecycle.__file__)"
.venv-monorepo-test/Scripts/python.exe -c "from miner.story_context_provider import TranscriptStoryContextProvider"
.venv-monorepo-test/Scripts/python.exe -c "from story_lifecycle.cli.main import cli"
```

全部通过。

## 测试验证

### story-miner

```bash
cd packages/story-miner
python -m pytest tests/ -v
```

结果：`6 skipped, 0 failed`
- 跳过的原因是 `data/transcripts.db` 不存在（该 db 含 PII，本就不入 git）。
- 无 import error、无 collection error。

### story-lifecycle

```bash
cd packages/story-lifecycle
python -m pytest tests/ -x --tb=short -q
```

结果：`625 passed, 2 skipped, 2 warnings`

### monorepo 根

```bash
python -m pytest --tb=short -q
```

结果：`625 passed, 8 skipped, 2 warnings`（8 skipped = story-lifecycle 2 + story-miner 6）

## CLI 验证

```bash
.venv-monorepo-test/Scripts/python.exe -m story_lifecycle --help
.venv-monorepo-test/Scripts/story.exe --help
```

`story` 入口命令可用。

## 未改动项

- `miner` 模块名保持不变（`import miner`），避免 story-lifecycle 动态加载器断裂。
- hc-all 业务硬编码（`WS_KEYWORDS`、输出到 hc-all/.story/knowledge/ 等）保留到 M6 泛化。
- 未提交 `.venv-monorepo-test/` 等测试环境文件。

## 结论

M2 验收通过：
- [x] `pip install -e packages/story-lifecycle` 成功
- [x] `pip install -e packages/story-miner` 成功
- [x] 两包 import 正常
- [x] story-lifecycle 625 测试通过
- [x] story-miner 6 测试因缺 db 跳过，无错误
- [x] monorepo 根 pytest 可一次跑完全部测试
