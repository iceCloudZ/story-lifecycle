export interface ParsedFileDiff {
  oldPath: string
  newPath: string
  oldContent: string
  newContent: string
  hunks: string[]
  raw: string
}

export function parseUnifiedDiff(diffText: string): ParsedFileDiff[] {
  if (!diffText.trim()) return []

  const files: ParsedFileDiff[] = []
  const parts = diffText.split(/(?=^diff --git)/m).filter(Boolean)

  for (const part of parts) {
    const lines = part.split('\n')
    if (lines.length < 3) continue

    const firstLine = lines[0]
    const match = firstLine.match(/^diff --git a\/(.+?) b\/(.+)$/)
    if (!match) continue

    const oldPath = match[1]
    const newPath = match[2]

    let hunkStart = -1
    for (let i = 1; i < lines.length; i++) {
      if (lines[i].startsWith('@@')) {
        hunkStart = i
        break
      }
    }
    if (hunkStart === -1) continue

    const oldLines: string[] = []
    const newLines: string[] = []
    const hunks: string[] = []
    let currentHunk: string[] = []

    for (let i = hunkStart; i < lines.length; i++) {
      const line = lines[i]
      if (line.startsWith('@@')) {
        if (currentHunk.length) {
          hunks.push(currentHunk.join('\n'))
        }
        currentHunk = [line]
      } else if (currentHunk.length) {
        currentHunk.push(line)
        if (line.startsWith('+') && !line.startsWith('+++')) {
          newLines.push(line.slice(1))
        } else if (line.startsWith('-') && !line.startsWith('---')) {
          oldLines.push(line.slice(1))
        } else if (line.startsWith(' ') || line === '') {
          oldLines.push(line.startsWith(' ') ? line.slice(1) : '')
          newLines.push(line.startsWith(' ') ? line.slice(1) : '')
        }
      }
    }
    if (currentHunk.length) {
      hunks.push(currentHunk.join('\n'))
    }

    files.push({
      oldPath,
      newPath,
      oldContent: oldLines.join('\n'),
      newContent: newLines.join('\n'),
      hunks,
      raw: part,
    })
  }

  return files
}
