# TEST-006 Test Story — Design

## 概述

Mock story，用于验证 story-lifecycle 的 design 阶段 handshake 流程。

## 复杂度

S — 仅新增文档文件，无代码改动。

## 影响范围

仅 `D:/story-lifecycle` 仓库本身，改动为：
- 新增 `docs/design-test-006.md`（本文档）
- 新增 `.story/done/TEST-006/design.json`（handshake 信号）

## 设计决策

- 不引入任何功能代码或依赖变更
- 设计文档内容保持最小，仅记录 story 元信息
- 通过成功写入 `.story/done/TEST-006/design.json` 验证 handshake 机制正常工作
