import { useEffect, useRef, useState, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import type { IDisposable } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { writeClipboard, makePtySender, type PtySender } from '../utils/ptyClipboard'
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
  // 带去重的 PTY 发送器:吸收 onData(P1)与容器 paste 冒泡(P3)对同一
  // 粘贴内容的重复触发。用 getWs 闭包始终指向最新 WS 句柄。
  const senderRef = useRef<PtySender | null>(null)
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

    // User input → PTY(粘贴主通道 P1:xterm 原生 paste 也走 onData)。
    // 走 sender 去重:与容器 paste 兜底(P3)对同一粘贴内容只发一次。
    const term = termRef.current
    if (term) {
      onDataDisposableRef.current?.dispose()
      onDataDisposableRef.current = term.onData((data) => {
        senderRef.current?.send(data)
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

    // 带去重的 PTY 发送器:用 getWs 闭包始终读最新 wsRef,WS 重连 / 切
    // session 后自动跟随,无需手动 dispose。
    senderRef.current = makePtySender(() => wsRef.current)

    // ── 复制粘贴 ── 统一走 utils/ptyClipboard 的真相源。xterm 默认不处理
    // copy/paste,需显式接管;历史上为 copy/paste 各注册了多条互不感知的
    // 入口,导致一次复制/粘贴落 3 份。现已收敛为单入口 + 发送去重。
    //
    // 复制:仅 Ctrl+C(有选区时)/ Ctrl+Shift+C / Cmd+C 一条路径。
    //   (已删 onSelectionChange「选中即复制」——拖选过程中每移动一个字符
    //   触发一次,与 Ctrl+C 叠加 + StrictMode 双挂载残留 → 最多 3 份。)
    //
    // 粘贴: onData(P1,xterm 原生 paste 通道)为主 + 容器 paste(P3)兜底,
    //   两路都走 sender,同内容 50ms 内只发一次。已删 customKey 的 Ctrl+V
    //   分支(P2)——它与 onData、容器 paste 三路叠加才是「粘贴 3 份」根因。
    term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey
      if (!mod) return true
      // Copy:Ctrl+C(有选区时)/Ctrl+Shift+C/Cmd+C。Ctrl+V 不在此拦截,
      // 让它走 xterm 默认 → onData(P1),避免与容器 paste 重复发送。
      if ((e.key === 'c' || e.key === 'C') && (e.shiftKey || term.hasSelection())) {
        const sel = term.getSelection()
        if (sel) { writeClipboard(sel); return false }
      }
      return true
    })

    // DOM paste 兜底(P3):helper textarea 丢失焦点时 onData 收不到 paste,
    // 靠容器上的 paste 事件兜住。走去重,与 onData(P1)对同一内容只发一次。
    const onPaste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData('text')
      if (text) {
        e.preventDefault()
        senderRef.current?.send(text)
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
      senderRef.current = null
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
