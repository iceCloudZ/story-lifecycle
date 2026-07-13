# ttyd 服务端 Web 终端

## 现状

`ttyd.py` 中 ttyd 相关代码（端口分配、`ensure_ttyd`、`stop_ttyd`）只有一个 REST API 端点调用，TUI 和编排器核心路径都不走它，处于废弃状态。

## 为什么保留

本地开发时不需要——`zellij attach` 或前台 Zellij layout 直接看本地终端就够了。

但服务端部署时，用户没有本地终端可 attach。ttyd 把 Zellij session 暴露为网页，用户浏览器就能实时观察 AI 工作过程、必要时介入。这是服务端部署的关键能力。

## 需要调整的

当前执行模型跟 ttyd 是脱节的：

- 实际执行走 `zellij_execution_args()`：生成一次性 Zellij layout，前台跑完 CLI 退出就结束
- `ensure_ttyd()` 创建的是交互式持久 session（`zellij attach --create`），但里面没有 AI 在跑

要让 ttyd 在服务端有用，需要统一成：**AI CLI 跑在持久 Zellij session → ttyd 暴露这个 session → 用户浏览器访问**。

## 额外考虑

- **安全**：当前 ttyd `--writable` 无认证。服务端部署需加 nginx 反代 + auth，或限制 `127.0.0.1` + SSH 隧道
- **生命周期**：session 创建/销毁时机需要重新设计（当前是被动触发）
- **建议**：在做服务端部署功能时一起重构，暂不删除现有 ttyd 代码
