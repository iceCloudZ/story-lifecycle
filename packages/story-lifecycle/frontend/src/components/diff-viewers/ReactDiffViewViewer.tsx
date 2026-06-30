import { parseDiff, Diff, Hunk } from 'react-diff-view'
import 'react-diff-view/style/index.css'
import type { ParsedFileDiff } from '../../utils/diffParser'

interface Props {
  file: ParsedFileDiff
}

export default function ReactDiffViewViewer({ file }: Props) {
  const files = parseDiff(file.raw)
  if (!files.length) return <div>无法解析 diff</div>

  return (
    <div className="preview-diff-viewer rdv-viewer">
      {files.map((f: any) => (
        <div key={f.oldRevision + '-' + f.newRevision} className="rdv-file">
          <div className="rdv-file-header">{f.newPath || f.oldPath}</div>
          <Diff viewType="unified" diffType={f.type} hunks={f.hunks}>
            {(hunks: any[]) => hunks.map((hunk: any) => <Hunk key={hunk.content} hunk={hunk} />)}
          </Diff>
        </div>
      ))}
    </div>
  )
}
