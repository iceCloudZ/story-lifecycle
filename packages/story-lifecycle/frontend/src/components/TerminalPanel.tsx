import { useEffect, useRef, useState, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import type { IDisposable } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import './TerminalPanel.css'

interface Props {
  storyKey: string | null
  autoConnect?: boolean
  sessionId?: string
}

type ConnectionState =
  | 'idle'
  | 'connecting'
  | 'streaming'
  | 'reconnecting'
  | 'exited'
  | 'not_found'
  | 'lost'

const MAX_RECONNECT_ATTEMPTS = 5
const BASE_RECONNECT_DELAY_MS = 1000
const MAX_RECONNECT_DELAY_MS = 30000

export default function TerminalPanel({ storyKey, autoConnect = false, sessionId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const onDataDisposableRef = useRef<IDisposable | null>(null)
  // When sessionId is provided, the session already exists — skip spawn
  const [spawned, setSpawned] = useState(!!sessionId)
  const [prevStoryKey, setPrevStoryKey] = useState(storyKey)
  const [connectionState, setConnectionState] = useState<ConnectionState>('idle')
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const shouldReconnectRef = useRef(true)
  // Sockets we are intentionally closing (session switch / unmount) should not
  // trigger the reconnect logic in their onclose handler.
  const closingWsRef = useRef<Set<WebSocket>>(new Set())
  // Ref indirection lets onclose reference connectWs without a TDZ self-reference.
  const connectWsRef = useRef<() => void>(() => {})

  // Spawn PTY on demand (only when sessionId not provided)
  const handleSpawn = useCallback(async () => {
    if (!storyKey || sessionId) return
    const r = await fetch(`/api/pty/${storyKey}/spawn`, { method: 'POST' })
    if (r.ok) setSpawned(true)
  }, [storyKey, sessionId])

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
    if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
      setConnectionState('lost')
      return
    }
    const delay = Math.min(
      BASE_RECONNECT_DELAY_MS * 2 ** reconnectAttemptsRef.current,
      MAX_RECONNECT_DELAY_MS
    )
    reconnectAttemptsRef.current += 1
    setConnectionState('reconnecting')
    reconnectTimerRef.current = setTimeout(() => connectWsRef.current(), delay)
  }, [])

  const connectWs = useCallback(() => {
    if (!storyKey || !spawned) return

    // Reset reconnect control for a fresh connection attempt.
    shouldReconnectRef.current = true
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
    // Drop any previous socket before opening a new one (handles session switch).
    if (wsRef.current) {
      closingWsRef.current.add(wsRef.current)
      try {
        wsRef.current.close()
      } catch {
        /* ignore */
      }
      wsRef.current = null
    }

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsPath = sessionId
      ? `${proto}//${location.host}/ws/pty/${storyKey}/${sessionId}`
      : `${proto}//${location.host}/ws/pty/${storyKey}`
    const ws = new WebSocket(wsPath)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws
    setConnectionState('connecting')

    ws.onopen = () => {
      reconnectAttemptsRef.current = 0
      setConnectionState('streaming')
    }

    ws.onclose = (event) => {
      const wasIntentional = closingWsRef.current.has(ws)
      closingWsRef.current.delete(ws)
      if (wsRef.current === ws) {
        wsRef.current = null
      }
      if (wasIntentional) {
        return
      }
      if (!shouldReconnectRef.current) {
        return
      }
      if (event.code === 1000) {
        shouldReconnectRef.current = false
        setConnectionState('exited')
        return
      }
      if (event.code === 4404) {
        shouldReconnectRef.current = false
        setConnectionState('not_found')
        return
      }
      scheduleReconnect()
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
            shouldReconnectRef.current = false
            termRef.current?.write('\r\n\x1b[33m[Process exited]\x1b[0m\r\n')
            setConnectionState('exited')
          } else if (msg.type === 'error' && msg.code === 'session_not_found') {
            shouldReconnectRef.current = false
            termRef.current?.write('\r\n\x1b[31m[Session not found]\x1b[0m\r\n')
            setConnectionState('not_found')
          }
        } catch {
          termRef.current?.write(event.data)
        }
      }
    }

    // User input → PTY
    const term = termRef.current
    if (term) {
      onDataDisposableRef.current?.dispose()
      onDataDisposableRef.current = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(data)
        }
      })
    }
  }, [storyKey, spawned, sessionId, scheduleReconnect])

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

    // BUG #11/#12:复制粘贴。xterm 默认不处理 copy/paste,需显式接管。
    // copy:有选区时 Ctrl+C/Ctrl+Shift+C/Cmd+C → 写剪贴板(clipboard API 不可用则 execCommand 兜底)。
    // paste:Ctrl+V/Cmd+V → 读剪贴板发 PTY;并在容器挂 paste 事件兜 DOM 粘贴。
    const writeClipboard = (text: string) => {
      try {
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text).catch(() => {})
        } else {
          // HTTP 非安全上下文兜底(clipboard API 为 undefined)
          const ta = document.createElement('textarea')
          ta.value = text
          document.body.appendChild(ta)
          ta.select()
          try { document.execCommand('copy') } catch { /* ignore */ }
          document.body.removeChild(ta)
        }
      } catch { /* ignore */ }
    }

    const sendToPty = (text: string) => {
      if (text && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(text)
      }
    }

    // 选区变化自动复制(保留原行为,加 try/catch 防 clipboard undefined 抛错)
    term.onSelectionChange(() => {
      const selection = term.getSelection()
      if (selection) writeClipboard(selection)
    })

    // 键盘兜底:拦截 copy/paste 快捷键,避免浏览器默认行为干扰终端
    term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey
      if (!mod) return true
      // Copy:Ctrl+C(有选区时)/Ctrl+Shift+C/Cmd+C
      if ((e.key === 'c' || e.key === 'C') && (e.shiftKey || term.hasSelection())) {
        const sel = term.getSelection()
        if (sel) { writeClipboard(sel); return false }
      }
      // Paste:Ctrl+V/Ctrl+Shift+V/Cmd+V
      if (e.key === 'v' || e.key === 'V') {
        try {
          navigator.clipboard?.readText?.().then((text) => { if (text) sendToPty(text) })
          return false
        } catch { return true }
      }
      return true
    })

    // DOM paste 兜底:在容器直接粘贴(不依赖 xterm helper textarea 获焦)
    const onPaste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData('text')
      if (text) {
        e.preventDefault()
        sendToPty(text)
      }
    }
    containerRef.current.addEventListener('paste', onPaste)

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
      containerRef.current?.removeEventListener('paste', onPaste)
      onDataDisposableRef.current?.dispose()
      onDataDisposableRef.current = null
      term.dispose()
      termRef.current = null
      fitRef.current = null
    }
  }, [storyKey, spawned])

  // Connect WebSocket when terminal is ready
  useEffect(() => {
    const closing = closingWsRef.current
    if (spawned && termRef.current) {
      shouldReconnectRef.current = true
      reconnectAttemptsRef.current = 0
      connectWs()
    }
    return () => {
      shouldReconnectRef.current = false
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (wsRef.current) {
        closing.add(wsRef.current)
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [spawned, connectWs])

  // Reset transient UI state when the story changes — render-time adjustment
  // (per https://react.dev/reference/react/useState#storing-information-from-previous-renders)
  // instead of a setState-in-effect. Also avoids clobbering the initial
  // `!!sessionId` state on first mount, which the previous effect did.
  if (storyKey !== prevStoryKey) {
    setPrevStoryKey(storyKey)
    setSpawned(false)
    setConnectionState('idle')
  }

  // Auto-connect for active stories. handleSpawn's setState runs only after an
  // awaited fetch, so it is not synchronous in this effect body.
  useEffect(() => {
    if (autoConnect && storyKey && !spawned) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      handleSpawn()
    }
  }, [autoConnect, storyKey, spawned, handleSpawn])

  const statusText: Record<ConnectionState, string> = {
    idle: '○ 空闲',
    connecting: '● 连接中...',
    streaming: '● 已连接',
    reconnecting: '🟡 重新连接中...',
    exited: '⚪ 进程已退出',
    not_found: '⚪ 会话不存在',
    lost: '🔴 连接丢失',
  }

  const showStartNew = connectionState === 'exited' || connectionState === 'not_found' || connectionState === 'lost'

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
        <span className={`ws-status ws-${connectionState}`}>
          {statusText[connectionState]}
        </span>
        <div className="terminal-toolbar-actions">
          {showStartNew && !sessionId && (
            <button className="toolbar-btn" onClick={handleSpawn}>
              重新启动
            </button>
          )}
        </div>
      </div>
      <div ref={containerRef} className="terminal-container" />
    </div>
  )
}
