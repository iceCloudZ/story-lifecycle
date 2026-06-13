import { useState, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { sessionApi } from '../api/client'

interface PTYSession {
  sessionId: string
  adapter: string
  stage: string
  model: string
  status: 'running' | 'waiting' | 'exited'
  startedAt: string
}

interface Props {
  storyKey: string
  autoConnect?: boolean
}

export default function usePTYSessions({ storyKey, autoConnect: _autoConnect = false }: Props) {
  const qc = useQueryClient()

  const { data: sessionList } = useQuery({
    queryKey: ['sessions', storyKey],
    queryFn: () => sessionApi.list(storyKey),
    enabled: !!storyKey,
    refetchInterval: 5000,
  })

  const sessions: PTYSession[] = (sessionList?.sessions ?? []).map((s) => ({
    sessionId: s.session_id,
    adapter: s.adapter,
    stage: s.stage,
    model: s.model,
    status: s.status as PTYSession['status'],
    startedAt: s.started_at,
  }))

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  // Auto-select first running session
  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      const running = sessions.find((s) => s.status !== 'exited')
      setActiveSessionId(running?.sessionId ?? sessions[0].sessionId)
    }
  }, [sessions, activeSessionId])

  const spawnSession = useCallback(
    async (adapter: string, model: string) => {
      const result = await sessionApi.spawn(storyKey, adapter, model)
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      setActiveSessionId(result.session_id)
    },
    [storyKey, qc]
  )

  const killSession = useCallback(
    async (sessionId: string) => {
      await sessionApi.kill(storyKey, sessionId)
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      if (activeSessionId === sessionId) {
        setActiveSessionId(null)
      }
    },
    [storyKey, qc, activeSessionId]
  )

  return {
    sessions,
    activeSessionId,
    setActiveSession: setActiveSessionId,
    spawnSession,
    killSession,
  }
}
