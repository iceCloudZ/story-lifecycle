import { useEffect, useRef, useState, useCallback } from 'react'

export interface StorySummary {
  storyKey: string
  title: string
  currentStage: string
  status: string
  profile: string
  executionCount: number
  updatedAt: string
}

export function useStories() {
  const [stories, setStories] = useState<StorySummary[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  const connect = useCallback(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/stories`)

    ws.onopen = () => setConnected(true)
    ws.onclose = () => {
      setConnected(false)
      setTimeout(connect, 3000)
    }
    ws.onerror = () => ws.close()

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'stories') {
          setStories(msg.data)
        } else if (msg.type === 'story_update') {
          // Refresh full list on incremental update
          fetch('/api/story')
            .then(r => r.json())
            .then(setStories)
            .catch(() => {})
        }
      } catch {}
    }

    wsRef.current = ws
  }, [])

  useEffect(() => {
    connect()
    return () => wsRef.current?.close()
  }, [connect])

  return { stories, connected }
}
