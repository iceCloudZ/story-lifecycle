# E2E 真实测试用例

## 环境

- zellij: 已安装 ✓
- claude CLI: 已安装 ✓
- LLM API: deepseek (已配置) ✓

## 准备：创建独立测试项目

```bash
mkdir -p /d/e2e-test && cd /d/e2e-test
```

写一个简单 PRD：

```bash
mkdir -p prd
cat > prd/E2E-HELLO.md << 'EOF'
## 需求：添加字符串工具模块

在项目中创建 `src/utils/strings.py`，实现以下函数：

1. `slugify(text: str) -> str` — 将任意文本转为 URL-safe slug
   - 英文：空格转 `-`，去除特殊字符，转小写
   - 中文：保留拼音或直接移除
   - 示例：`"Hello World!"` → `"hello-world"`

2. `truncate(text: str, max_len: int = 50, suffix: str = "...") -> str`
   - 截断超长文本，末尾加 suffix
   - 示例：`truncate("很长的文本...", 10)` → `"很长的文本..."`

在 `tests/test_strings.py` 添加对应的单元测试。

完成后在项目根目录创建 `.story-done/E2E-HELLO/{stage}.json` 标记完成。
EOF
```

## 测试 1: Happy Path — design → implement → test

### 创建 Story

```bash
cd /d/e2e-test
story new E2E-HELLO --title "添加字符串工具模块" -c prd/E2E-HELLO.md
```

预期输出：
```
Story created: E2E-HELLO
  Stage: design
  Workspace: /d/e2e-test
```

### 进入 Board 启动执行

```bash
story board
```

TUI 操作：
1. 看到 E2E-HELLO，stage=design，status=active
2. 按 **e** — 进入终端，Claude Code 会自动启动并收到 prompt
3. Claude Code 开始执行 design 阶段（分析需求、写设计文档）
4. Claude Code 完成后会写入 `.story-done/E2E-HELLO/design.json`
5. Watchdog 检测到文件 → 自动推进到 implement 阶段
6. 按 **q** 退出终端，回到 board 看状态变化

如果 watchdog 没自动推进，按 **R** 手动刷新。

### 手动推进（备选）

如果不想等 watchdog，在另一个终端：

```bash
# 查看 Claude Code 是否完成了 design
cat /d/e2e-test/.story-done/E2E-HELLO/design.json

# 手动推进
story resume E2E-HELLO
```

### 逐阶段观察

每个阶段完成后重复：
1. `story board` 看 board 状态
2. 按 **e** 进入终端执行
3. 等 Claude Code 完成
4. watchdog 自动推进

三个阶段完成后，最终状态：

```
E2E-HELLO  添加字符串工具模块  test  OK done
```

### 查看完整日志

```bash
story log E2E-HELLO
```

预期看到：plan → execute → poll → review → advance → plan → execute → ... → complete

## 测试 2: Skip 跳过阶段

```bash
cd /d/e2e-test
story new E2E-SKIP --title "Skip测试"

# 在 board 中按 s 跳过当前阶段
story board
# 选中 E2E-SKIP，按 s
```

预期：stage 从 design 跳到 implement

## 测试 3: Fail + Resume

```bash
story new E2E-FAIL --title "Fail测试"

# 在 board 中按 f 标记失败
story board
# 选中 E2E-FAIL，按 f

# 确认 blocked 状态
# 按 r 恢复为 active
```

## 测试 4: 手动 CLI（不走 TUI）

适合调试，直接命令行操作：

```bash
cd /d/e2e-test

# 创建 story
story new E2E-CLI --title "CLI测试"

# 查看状态
story status E2E-CLI

# 手动模拟完成（跳过真实 AI 执行）
mkdir -p .story-done/E2E-CLI
echo '{"spec_path":"docs/test.md","complexity":"S","summary":"测试"}' > .story-done/E2E-CLI/design.json

# 推进
story resume E2E-CLI
story status E2E-CLI   # 应变为 stage=implement
```

## 清理

```bash
rm -rf /d/e2e-test
rm -f ~/.story-lifecycle/story.db
```
