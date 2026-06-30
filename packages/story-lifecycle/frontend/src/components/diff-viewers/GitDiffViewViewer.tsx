import { DiffView, DiffModeEnum } from '@git-diff-view/react'
import '@git-diff-view/react/styles/diff-view.css'
import type { ParsedFileDiff } from '../../utils/diffParser'

interface Props {
  file: ParsedFileDiff
}

export default function GitDiffViewViewer({ file }: Props) {
  return (
    <div className="preview-diff-viewer">
      <DiffView
        data={{
          oldFile: { fileName: file.oldPath, content: file.oldContent },
          newFile: { fileName: file.newPath, content: file.newContent },
          hunks: [file.raw],
        }}
        diffViewMode={DiffModeEnum.Unified}
        diffViewTheme="light"
        diffViewHighlight
        diffViewWrap
      />
    </div>
  )
}
