import { useEffect, useRef, useCallback } from 'react'
import { useStoryStore } from '../store/storyStore'

export function useStoryWebSocket() {
  const { setStories, setConnected, updateStory } = useStoryStore()
  const wsRef = useRef<WebSocket | null>(null)
  // Ref indirection lets onclose reference "connect" without a TDZ self-reference.
  const connectRef = useRef<() => void>(() => {})

  const connect = useCallback(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/stories`)

    ws.onopen = () => setConnected(true)
    ws.onclose = () => {
      setConnected(false)
      setTimeout(() => connectRef.current(), 3000)
    }
    ws.onerror = () => ws.close()

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'stories') {
          setStories(msg.data)
        } else if (msg.type === 'story_update') {
          const d = msg.data
          updateStory(d.storyKey, {
            status: d.status,
            currentStage: d.currentStage,
          })
          // Also refresh full list for consistency
          fetch('/api/story')
            .then((r) => r.json())
            .then(setStories)
            .catch(() => {})
        }
      } catch {
        /* ignore parse errors */
      }
    }

    wsRef.current = ws
  }, [setStories, setConnected, updateStory])

  // Keep the reconnect ref pointed at the latest connect (in an effect, not
  // during render). Declared before the connect() effect so the ref is set
  // before the first connection is opened.
  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  useEffect(() => {
    connect()
    return () => wsRef.current?.close()
  }, [connect])
}
