import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
// highlight.js github 主题(代码块语法高亮配色)。全局 import 一次即可。
import 'highlight.js/styles/github.css'
import './markdown.css'

/**
 * MarkdownView — 统一的 markdown 渲染组件。
 *
 * - remark-gfm:表格 / 删除线 / 任务列表 / 自动链接
 * - rehype-highlight:代码块语法高亮(highlight.js github 主题)
 * - .markdown-body:GitHub 风格排版(标题 / 段落 / 代码 / 表格 / 引用)
 *
 * DocEditor 预览 + 未来其它只读展示场景复用。
 */
export default function MarkdownView({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {content}
      </ReactMarkdown>
    </div>
  )
}
