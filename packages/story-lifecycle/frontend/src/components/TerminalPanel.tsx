import { useEffect, useRef, useState, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import './TerminalPanel.css'

interface Props {
  storyKey: string | null
  autoConnect?: boolean
  sessionId?: string
}

export default function TerminalPanel({ storyKey, autoConnect = false, sessionId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  // When sessionId is provided, the session already exists — skip spawn
  const [spawned, setSpawned] = useState(!!sessionId)
  const [prevStoryKey, setPrevStoryKey] = useState(storyKey)
  const [searchVisible, setSearchVisible] = useState(false)
  const [searchText, setSearchText] = useState('')
  const [wsStatus, setWsStatus] = useState<'disconnected' | 'connecting' | 'connected'>('disconnected')
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Ref indirection lets onclose reference connectWs without a TDZ self-reference.
  const connectWsRef = useRef<() => void>(() => {})

  // Spawn PTY on demand (only when sessionId not provided)
  const handleSpawn = useCallback(async () => {
    if (!storyKey || sessionId) return
    const r = await fetch(`/api/pty/${storyKey}/spawn`, { method: 'POST' })
    if (r.ok) setSpawned(true)
  }, [storyKey, sessionId])

  const connectWs = useCallback(() => {
    if (!storyKey || !spawned) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsPath = sessionId
      ? `${proto}//${location.host}/ws/pty/${storyKey}/${sessionId}`
      : `${proto}//${location.host}/ws/pty/${storyKey}`
    const ws = new WebSocket(wsPath)
    ws.binaryType = 'arraybuffer'
    setWsStatus('connecting')

    ws.onopen = () => {
      setWsStatus('connected')
    }

    ws.onclose = () => {
      setWsStatus('disconnected')
      // Auto-reconnect after 3 seconds
      if (spawned) {
        reconnectTimerRef.current = setTimeout(() => connectWsRef.current(), 3000)
      }
    }

    ws.onerror = () => {
      ws.close()
    }

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        termRef.current?.write(new Uint8Array(event.data))
      } else if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data)
          if (msg.type === 'exit') {
            termRef.current?.write('\r\n\x1b[33m[Process exited]\x1b[0m\r\n')
          }
        } catch {
          termRef.current?.write(event.data)
        }
      }
    }

    // User input → PTY
    const term = termRef.current
    if (term) {
      term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(data)
        }
      })
    }

    wsRef.current = ws
  }, [storyKey, spawned, sessionId])

  // Keep the reconnect ref pointed at the latest connectWs (in an effect, not
  // during render).
  useEffect(() => {
    connectWsRef.current = connectWs
  }, [connectWs])

  // Initialize terminal
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

    // Copy selection to clipboard
    term.onSelectionChange(() => {
      const selection = term.getSelection()
      if (selection) {
        navigator.clipboard.writeText(selection).catch(() => {})
      }
    })

    // Resize → PTY
    term.onResize(({ cols, rows }) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })

    // Handle window resize
    const onResize = () => fit.fit()
    window.addEventListener('resize', onResize)

    // Refit when the container itself resizes — covers the panel mounting
    // before layout settles (e.g. tab opened in the background → 0-width at
    // open). Without this xterm stays at the initial tiny cols, the PTY gets
    // resized to a garbling width, and the agent TUI renders at ~2 cols.
    let raf = 0
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        try {
          fit.fit()
        } catch {
          /* container disposed mid-callback */
        }
      })
    })
    ro.observe(containerRef.current)

    return () => {
      window.removeEventListener('resize', onResize)
      ro.disconnect()
      cancelAnimationFrame(raf)
      term.dispose()
      termRef.current = null
      fitRef.current = null
    }
  }, [storyKey, spawned])

  // Connect WebSocket when terminal is ready
  useEffect(() => {
    if (spawned && termRef.current) {
      connectWs()
    }
    return () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
      }
      wsRef.current?.close()
      wsRef.current = null
    }
  }, [spawned, connectWs])

  // Reset transient UI state when the story changes — render-time adjustment
  // (per https://react.dev/reference/react/useState#storing-information-from-previous-renders)
  // instead of a setState-in-effect. Also avoids clobbering the initial
  // `!!sessionId` state on first mount, which the previous effect did.
  if (storyKey !== prevStoryKey) {
    setPrevStoryKey(storyKey)
    setSpawned(false)
    setSearchVisible(false)
  }

  // Auto-connect for active stories. handleSpawn's setState runs only after an
  // awaited fetch, so it is not synchronous in this effect body.
  useEffect(() => {
    if (autoConnect && storyKey && !spawned) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      handleSpawn()
    }
  }, [autoConnect, storyKey, spawned, handleSpawn])

  // Search functionality
  function handleSearch() {
    if (!searchText || !termRef.current) return
    // Basic search: write the search text to terminal for visual scan
    // Full search requires @xterm/addon-search which can be added later
  }

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

  return (
    <div className="terminal-wrapper">
      <div className="terminal-toolbar">
        <span className={`ws-status ws-${wsStatus}`}>
          {wsStatus === 'connected' ? '● 已连接' : wsStatus === 'connecting' ? '● 连接中...' : '○ 断开连接'}
        </span>
        <button
          className="toolbar-btn"
          onClick={() => setSearchVisible(!searchVisible)}
          title="搜索"
        >
          🔍
        </button>
      </div>
      {searchVisible && (
        <div className="terminal-search">
          <input
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleSearch() }}
            placeholder="搜索终端输出..."
            className="search-input"
          />
          <button className="toolbar-btn" onClick={handleSearch}>查找</button>
          <button className="toolbar-btn" onClick={() => setSearchVisible(false)}>✕</button>
        </div>
      )}
      <div ref={containerRef} className="terminal-container" />
    </div>
  )
}
