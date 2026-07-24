# AGENTS.md — story-lifecycle frontend

React 19 + TypeScript + Vite。**改完必须 `npm run build`**(输出到
`../src/story_lifecycle/entry/web`,后端 8180 直接服务构建产物;仓库惯例是构建产物随代码一起提交)。

## UI 规范(所有页面必须遵守)

设计语言:Linear 风浅色主题 —— zinc 中性灰 + indigo 主色、柔和阴影、6–12px 圆角、弱色小字小节标题。

### 1. 只用 design token,禁止硬编码

- 颜色/间距/圆角/字号/字体一律 `var(--*)`,定义在 `src/styles/tokens.css`。
- 禁止在组件 CSS 或 TSX 里写死 hex 色值、px 字号、`ui-monospace, ...` 字体栈
  (等宽用 `var(--font-mono)`)。历史上 `CodeChangesTab.css` 整文件硬编码,是反例。
- 禁止内联 `style={{ color: '#...' }}`;需要着色就加语义类(如 `.ui-diff-add`)。

### 2. 通用原语:优先用 `src/styles/ui.css` 的 `.ui-*` 类

跨组件复用的模式只在 `ui.css` 定义一次,组件 CSS 里不得重复造:

| 类 | 用途 |
|---|---|
| `.ui-card` / `.ui-card-pad` | 内容区块的标准容器(白底 + 边框 + radius-lg + 柔阴影) |
| `.ui-section-title` | 区块小节标题(弱色小字 + 宽字距),卡片/面板标题统一用它 |
| `.ui-hint` | 辅助说明小字(替代旧的 `.hint`,它从未全局定义过) |
| `.ui-alert` + `-danger/-warning/-info` | 告警/提示条(图标 + 软底 + 细边框) |
| `.ui-empty` | 空态(虚线占位面板):无数据/无会话/无变更统一用它 |
| `.ui-chip` + `.active` | 胶囊切换(分段控件):tab/项目/过滤器切换统一用它 |
| `.ui-stats` / `.ui-stat` | 统计数字块 |
| `.ui-diff-add` / `.ui-diff-del` | diff 增删计数颜色 |
| `.ui-list` / `.ui-list-row` (+`.clickable`) | 卡片内的分隔列表行 |

按钮(`.btn` 系列)、徽章(`.badge` 系列)、原生表单控件样式在 `App.css` 全局定义,直接用。
组件专属布局(如终端分栏、步骤条)才写组件自己的 `.css`,BEM 式前缀(`cct-*`、`ot-*`)。

新增一个跨组件模式时:先加进 `ui.css` 再使用,并更新上表。

### 3. 图标:统一描边 SVG,禁止 emoji

- UI 骨架(导航、按钮、状态标识)一律用内联 SVG:`viewBox="0 0 16 16"`、
  `stroke="currentColor"`、`strokeWidth 1.4–1.5`、圆角线帽,颜色继承文字色。
- 禁止用 emoji 当图标(📊📦🤖💻 等)——渲染尺寸/风格不可控,是旧 UI 显廉价的主因。
- 状态用 7–8px 彩色圆点表达(语义色:success/warning/primary/gray),不用 emoji 圆点。
- 数据自带的 icon 字段(如 action.icon)可以保留,但 UI 骨架不新增 emoji。

### 4. 状态与层级

- 语义色三分组:solid(实心)/`-soft`(软底)/`-text`(文字),按用途选,不混造新色。
- 页面层级:灰底(`--color-bg`)→ 白卡片(`ui-card`)→ 卡片内灰底区块(`--gray-50/100`)。
  不要在卡片上再叠白卡片。
- 待操作状态可用呼吸动画(参考 `.ss-dot-pulse`、`.ot-node-ring`),已完成/静默状态不动。

### 5. 文字

- 标题层级:页面标题 `--text-lg` → 小节标题 `.ui-section-title` → 正文 `--text-base/sm`。
- 路径、分支名、版本号、数字统计用 `var(--font-mono)`。
- 中文正文不动词大写化;小节标题靠 `letter-spacing: 0.08em` + 弱色建立层级,不靠加粗加大。
