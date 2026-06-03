import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import './TerminalPanel.css'

interface Props {
  storyKey: string | null
}

export default function TerminalPanel({ storyKey }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const [spawned, setSpawned] = useState(false)

  // Spawn PTY on demand
  async function handleSpawn() {
    if (!storyKey) return
    const r = await fetch(`/api/pty/${storyKey}/spawn`, { method: 'POST' })
    if (r.ok) setSpawned(true)
  }

  // Connect xterm to PTY WebSocket when storyKey/spawned changes
  useEffect(() => {
    if (!containerRef.current || !storyKey || !spawned) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: 'Cascadia Code, Fira Code, Consolas, monospace',
      theme: {
        background: '#1a1a2e',
        foreground: '#e0e0e0',
        cursor: '#00d2ff',
        selectionBackground: '#264f78',
      },
    })

    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)
    fit.fit()

    termRef.current = term
    fitRef.current = fit

    // Connect WS
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/pty/${storyKey}`)
    ws.binaryType = 'arraybuffer'

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(event.data))
      } else if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data)
          if (msg.type === 'exit') {
            term.write('\r\n\x1b[33m[Process exited]\x1b[0m\r\n')
          }
        } catch {
          term.write(event.data)
        }
      }
    }

    // User input → PTY
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(data)
      }
    })

    // Resize → PTY
    term.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })

    wsRef.current = ws

    // Handle window resize
    const onResize = () => fit.fit()
    window.addEventListener('resize', onResize)

    return () => {
      window.removeEventListener('resize', onResize)
      ws.close()
      term.dispose()
      termRef.current = null
      wsRef.current = null
    }
  }, [storyKey, spawned])

  // Cleanup spawned state when story changes
  useEffect(() => {
    setSpawned(false)
  }, [storyKey])

  if (!storyKey) {
    return <div className="terminal-empty">选择一个 Story 后启动终端</div>
  }

  if (!spawned) {
    return (
      <div className="terminal-empty">
        <button className="spawn-btn" onClick={handleSpawn}>
          启动终端
        </button>
      </div>
    )
  }

  return <div ref={containerRef} className="terminal-container" />
}
