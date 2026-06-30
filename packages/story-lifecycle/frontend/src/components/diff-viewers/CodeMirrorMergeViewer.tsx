import { useEffect, useRef } from 'react'
import { MergeView } from '@codemirror/merge'
import { EditorView } from '@codemirror/view'
import { basicSetup } from 'codemirror'
import type { ParsedFileDiff } from '../../utils/diffParser'

interface Props {
  file: ParsedFileDiff
}

export default function CodeMirrorMergeViewer({ file }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<MergeView | null>(null)

  useEffect(() => {
    if (!containerRef.current) return

    const view = new MergeView({
      a: {
        doc: file.oldContent,
        extensions: [basicSetup, EditorView.editable.of(false)],
      },
      b: {
        doc: file.newContent,
        extensions: [basicSetup, EditorView.editable.of(false)],
      },
      parent: containerRef.current,
      highlightChanges: true,
      gutter: true,
    })
    viewRef.current = view

    return () => {
      view.destroy()
      viewRef.current = null
    }
  }, [file])

  return <div ref={containerRef} className="preview-diff-viewer cm-merge-viewer" />
}
