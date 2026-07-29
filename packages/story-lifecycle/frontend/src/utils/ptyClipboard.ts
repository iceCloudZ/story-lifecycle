// PTY 终端剪贴板访问 + 发送去重 —— 终端 copy/paste 的唯一真相源。
//
// 为什么需要这个模块:TerminalPanel 曾为「复制」和「粘贴」各注册了多条
// 互不感知的入口(复制:onSelectionChange + Ctrl+C;粘贴:onData +
// customKey Ctrl+V + 容器 paste 冒泡),同一份内容被写/发 N 次。本模块
// 集中剪贴板读写,并为发送提供时间窗去重,从结构上消除重复。

// 写剪贴板:优先 navigator.clipboard.writeText;HTTP 非安全上下文下
// clipboard API 为 undefined,用临时 textarea + execCommand 兜底。
export function writeClipboard(text: string): void {
  try {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).catch(() => {})
      return
    }
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } catch { /* ignore */ }
    document.body.removeChild(ta)
  } catch { /* ignore */ }
}

// 读剪贴板:成功 resolve 文本;失败 / API 不可用 resolve ''(不抛)。
export function readClipboard(): Promise<string> {
  try {
    return Promise.resolve(navigator.clipboard?.readText?.() ?? '')
  } catch {
    return Promise.resolve('')
  }
}

export interface PtySender {
  // 发送文本到 PTY(经 WebSocket)。同内容在 dedupMs 时间窗内只发一次,
  // 用于吸收 onData 与容器 paste 冒泡对同一粘贴内容的重复触发。
  send(text: string): void
}

// 构造一个带去重的 PTY 发送器。getWs 用函数形式,避免捕获到旧的 WS
// 句柄——重连 / 切 session 后 wsRef 变了,getWs() 始终返回最新的。
//
// dedupMs 默认 50ms:xterm 内部 paste 与冒泡 paste 事件几乎同步触发,
// 而用户连发按键的典型间隔远大于 50ms,误杀风险低。
export function makePtySender(getWs: () => WebSocket | null, dedupMs = 50): PtySender {
  let lastText = ''
  let lastTs = 0
  return {
    send(text: string) {
      if (!text) return
      const ws = getWs()
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      const now = Date.now()
      if (text === lastText && now - lastTs < dedupMs) return
      lastText = text
      lastTs = now
      ws.send(text)
    },
  }
}
