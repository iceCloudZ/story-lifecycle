import { useQuery } from '@tanstack/react-query'
import { storyApi } from '../api/client'
import { useStoryStore, type StorySummary } from '../store/storyStore'

/**
 * 统一的 story 列表数据源(hook)。
 *
 * 统一 Zustand store(WS 实时推送)+ TanStack Query(REST 轮询),替掉之前 Dashboard
 * 用 store 而 ReleaseTrainBoard/DiagnosticsPage 各自 useQuery 没用 store 的双轨不一致。
 * 所有需要 story 列表的页面都走这个 hook,保证单一真相源。
 */
export function useStories(refetchMs = 10000): {
  stories: StorySummary[]
  isLoading: boolean
} {
  const { stories: initial } = useStoryStore()
  const { data, isLoading } = useQuery({
    queryKey: ['stories'],
    queryFn: storyApi.list,
    initialData: initial,
    refetchInterval: refetchMs,
  })
  return { stories: data ?? [], isLoading }
}
