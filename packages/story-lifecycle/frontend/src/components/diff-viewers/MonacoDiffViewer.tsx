import { DiffEditor } from '@monaco-editor/react'
import type { ParsedFileDiff } from '../../utils/diffParser'

interface Props {
  file: ParsedFileDiff
}

function detectLang(path: string): string {
  if (path.endsWith('.java')) return 'java'
  if (path.endsWith('.ts') || path.endsWith('.tsx')) return 'typescript'
  if (path.endsWith('.js') || path.endsWith('.jsx')) return 'javascript'
  if (path.endsWith('.xml')) return 'xml'
  if (path.endsWith('.md')) return 'markdown'
  if (path.endsWith('.json')) return 'json'
  if (path.endsWith('.py')) return 'python'
  if (path.endsWith('.yaml') || path.endsWith('.yml')) return 'yaml'
  return 'text'
}

export default function MonacoDiffViewer({ file }: Props) {
  const lang = detectLang(file.newPath || file.oldPath)
  return (
    <div className="preview-diff-viewer monaco-viewer">
      <DiffEditor
        height="600px"
        original={file.oldContent}
        modified={file.newContent}
        language={lang}
        options={{
          readOnly: true,
          renderSideBySide: false,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
        }}
      />
    </div>
  )
}
