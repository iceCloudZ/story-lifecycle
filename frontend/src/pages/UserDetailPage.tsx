import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { contactApi } from '../api/client'
import './UserDetailPage.css'

interface ChannelResult {
  channel: string
  reachable: boolean
  score: number
  error_code: string
  error_message: string
  detail: string
}

interface ReachabilityResult {
  contact_id: string
  overall_reachable: boolean
  fully_reachable: boolean
  channels: Record<string, ChannelResult>
  third_party_verified?: boolean
  provider_name?: string
  checked_at: string
}

interface HistoryCheck {
  id: number
  overall_reachable: boolean
  fully_reachable: boolean
  channels: Record<string, ChannelResult>
  local_check_only: boolean
  provider_name: string
  checked_at: string
}

const CHANNEL_LABELS: Record<string, string> = {
  existence: '存在性',
  email: '邮箱',
  phone: '手机',
  sms: '短信',
}

export default function UserDetailPage() {
  const { contactId } = useParams<{ contactId: string }>()
  const qc = useQueryClient()
  const [verifyWithProvider, setVerifyWithProvider] = useState(false)
  const [showHistory, setShowHistory] = useState(false)

  const checkMutation = useMutation({
    mutationFn: () =>
      contactApi.checkReachability({
        contact_id: contactId || '',
        verify_with_provider: verifyWithProvider,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['reachability', contactId] }),
  })

  const { data: historyData } = useQuery({
    queryKey: ['reachability-history', contactId],
    queryFn: () => contactApi.getHistory(contactId || ''),
    enabled: showHistory && !!contactId,
  })

  const { data: lastResult } = useQuery({
    queryKey: ['reachability', contactId],
    queryFn: async () => {
      const hist = await contactApi.getHistory(contactId || '', 1)
      return hist.checks?.[0] as HistoryCheck | undefined
    },
    enabled: !!contactId,
  })

  const result: ReachabilityResult | undefined = checkMutation.data
  const historyChecks: HistoryCheck[] = historyData?.checks || []

  return (
    <div className="user-detail-page">
      <div className="user-detail-header">
        <h2>联系人: {contactId}</h2>
      </div>

      <div className="reachability-panel">
        <div className="reachability-actions">
          <button
            className="btn btn-check"
            onClick={() => checkMutation.mutate()}
            disabled={checkMutation.isPending}
          >
            {checkMutation.isPending ? '校验中...' : '校验可联性'}
          </button>
          <label className="verify-toggle">
            <input
              type="checkbox"
              checked={verifyWithProvider}
              onChange={(e) => setVerifyWithProvider(e.target.checked)}
            />
            第三方验证
          </label>
          <button
            className="btn btn-secondary"
            onClick={() => setShowHistory(!showHistory)}
          >
            {showHistory ? '隐藏历史' : '查看历史'}
          </button>
        </div>

        {checkMutation.isError && (
          <div className="reachability-error">
            校验失败: {(checkMutation.error as Error).message}
          </div>
        )}

        {(result || lastResult) && (
          <ReachabilityResultPanel result={(result || lastResult)!} />
        )}

        {showHistory && (
          <div className="history-panel">
            <h3>校验历史</h3>
            {historyChecks.length === 0 ? (
              <p className="history-empty">暂无历史记录</p>
            ) : (
              <div className="history-timeline">
                {historyChecks.map((check) => (
                  <div key={check.id} className="history-item">
                    <div className="history-time">
                      {check.checked_at ? new Date(check.checked_at).toLocaleString() : ''}
                    </div>
                    <div className="history-status">
                      <span className={`status-badge ${check.overall_reachable ? 'status-ok' : 'status-fail'}`}>
                        {check.overall_reachable ? '可达' : '不可达'}
                      </span>
                      <span className={`status-badge ${check.fully_reachable ? 'status-ok' : 'status-partial'}`}>
                        {check.fully_reachable ? '完全可达' : '部分可达'}
                      </span>
                      {!check.local_check_only && (
                        <span className="status-badge status-provider">第三方验证</span>
                      )}
                    </div>
                    <ChannelBadges channels={check.channels} />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function ReachabilityResultPanel({ result }: { result: ReachabilityResult | HistoryCheck }) {
  const channels = result.channels || {}
  const channelEntries = Object.entries(channels)

  return (
    <div className="result-panel">
      <div className="result-summary">
        <span className={`status-badge ${result.overall_reachable ? 'status-ok' : 'status-fail'}`}>
          {result.overall_reachable ? '可达' : '不可达'}
        </span>
        <span className={`status-badge ${result.fully_reachable ? 'status-ok' : 'status-partial'}`}>
          {result.fully_reachable ? '全部渠道可达' : '部分渠道不可达'}
        </span>
      </div>

      <div className="channel-cards">
        {channelEntries.map(([name, ch]) => (
          <div key={name} className={`channel-card ${ch.reachable ? 'card-ok' : 'card-fail'}`}>
            <div className="channel-header">
              <span className="channel-icon">{ch.reachable ? '✅' : '❌'}</span>
              <span className="channel-name">{CHANNEL_LABELS[name] || name}</span>
            </div>
            <div className="channel-detail">{ch.detail}</div>
            {ch.score > 0 && <div className="channel-score">score: {ch.score.toFixed(2)}</div>}
            {ch.error_code && <div className="channel-error">{ch.error_code}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}

function ChannelBadges({ channels }: { channels: Record<string, ChannelResult> }) {
  return (
    <div className="channel-badges">
      {Object.entries(channels || {}).map(([name, ch]) => (
        <span key={name} className={`channel-badge ${ch.reachable ? 'badge-ok' : 'badge-fail'}`}>
          {CHANNEL_LABELS[name] || name}
        </span>
      ))}
    </div>
  )
}
