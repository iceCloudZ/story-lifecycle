import { useState, useCallback } from 'react'
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

export default function usePTYSessions({ storyKey }: Props) {
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

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)

  // Effective active session: explicit selection, else auto-pick the first
  // running session (derived during render instead of setState-in-effect).
  const activeSessionId =
    selectedSessionId ??
    (sessions.length > 0
      ? (sessions.find((s) => s.status !== 'exited') ?? sessions[0]).sessionId
      : null)

  const spawnSession = useCallback(
    async (adapter: string, model: string) => {
      const result = await sessionApi.spawn(storyKey, adapter, model)
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      setSelectedSessionId(result.session_id)
    },
    [storyKey, qc, setSelectedSessionId]
  )

  const killSession = useCallback(
    async (sessionId: string) => {
      await sessionApi.kill(storyKey, sessionId)
      qc.invalidateQueries({ queryKey: ['sessions', storyKey] })
      if (selectedSessionId === sessionId) {
        setSelectedSessionId(null)
      }
    },
    [storyKey, qc, selectedSessionId, setSelectedSessionId]
  )

  return {
    sessions,
    activeSessionId,
    setActiveSession: setSelectedSessionId,
    spawnSession,
    killSession,
  }
}
