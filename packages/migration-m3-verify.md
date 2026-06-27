# M3 验证报告：跨项目契约测试

## 目标

在 `tests/contracts/` 建立 4 组跨项目契约测试，锁定 story-lifecycle 与 miner 之间的接口：
1. anchors 写读
2. provider 协议
3. done_cmd → retrospect
4. store-link schema 解耦

## 新增 / 修改文件

| 文件 | 说明 |
|------|------|
| `packages/story-miner/miner/anchors.py` | 新增：读取 `<ws>/.story/runs/<story_key>/anchors.jsonl`，验证必填字段 |
| `packages/story-miner/miner/store.py` | 新增 `init_db(db_path=None)`，便于契约测试初始化 schema |
| `packages/story-miner/miner/story_ingest.py` | 新增 `init_db(db_path=None)`，便于契约测试初始化 stories 表 |
| `tests/contracts/test_anchors_contract.py` | anchors 写读契约 |
| `tests/contracts/test_provider_contract.py` | provider 协议契约 |
| `tests/contracts/test_done_retrospect_contract.py` | done_cmd → retrospect 调用契约 |
| `tests/contracts/test_store_link_schema_contract.py` | store / story_ingest / link schema 契约 |
| `pyproject.toml` | 根 testpaths 加入 `tests/contracts` |

## 契约详情

### 1. anchors 写读契约

story-lifecycle `BaseAdapter.write_anchor` 输出 JSONL：
```json
{"story_key": "...", "stage": "...", "adapter": "...", "cwd": "...", "ts": "...", "prompt_hash": "..."}
```

miner `read_anchors(workspace, story_key)` 必须能解析这些记录，跳过损坏/缺字段行。

测试覆盖：
- story-lifecycle 写出全部必填字段
- miner 完整读回多行 anchors
- miner 跳过 malformed 行
- miner 对不存在的 anchor 文件返回空列表

### 2. provider 协议契约

接口签名：
```python
get_context(story_key: str, workspace: str, stage: str) -> str | None
```

测试使用 committed fixture 内存数据库（stories + sessions + events），覆盖：
- 已知 story 返回非空 str
- 未知 story 返回 None
- 永不抛异常
- 输出不脱敏手机号/邮箱
- 接受 `tapd-` 前缀的 story_key

### 3. done_cmd → retrospect 契约

story-lifecycle `done_cmd` 通过 `list_cmd._run_miner_retrospect` 调用：
```bash
python packages/story-miner/scripts/retrospect.py --story <story_key>
```

测试覆盖：
- 正确构造命令行（`python` + 脚本路径 + `--story` + story_key）
- 脚本缺失时静默跳过、不崩溃

### 4. store-link schema 解耦

- `miner.store.init_db()` 创建 sessions / events / sources 表
- `miner.story_ingest.init_db()` 创建 stories 表
- `miner.link` 期望的列全部存在
- `sessions.story_id` 可在运行时被 `ALTER TABLE ADD COLUMN`

## 运行结果

### 契约测试单独运行

```bash
python -m pytest tests/contracts -v
```

结果：`14 passed in 1.01s`

### 全量测试（monorepo 根）

```bash
python -m pytest --tb=short -q
```

结果：`639 passed, 8 skipped, 2 warnings in 100.17s`

- 639 = story-lifecycle 625 + story-miner 0（无 db 时跳过）+ contracts 14
- 8 skipped = story-miner 6（缺 transcripts.db）+ story-lifecycle 2

## 双向断言说明

- **anchors**：story-lifecycle 写出的字段集合必须与 miner `REQUIRED_KEYS` 匹配；miner 读取结果必须包含 story-lifecycle 写出的值。
- **provider**：story-lifecycle 调用方假设 `str | None` 返回；miner 提供方必须实现该语义。
- **done-retrospect**：story-lifecycle 调用方构造的命令必须与 miner 脚本期望的 `--story` 参数匹配。
- **store-link**：store/story_ingest 创建的列名集合必须包含 link 查询的列名集合。

## 结论

M3 验收通过：
- [x] `tests/contracts/` 下 4 组契约测试就位
- [x] 14 个契约测试全部通过
- [x] 全量测试 639 passed / 8 skipped
- [x] story-lifecycle 改 anchors 字段 → anchors 契约测试失败
- [x] miner 改 read_anchors 必填字段 → anchors 契约测试失败
- [x] CI 可从 monorepo 根一键跑全链路
