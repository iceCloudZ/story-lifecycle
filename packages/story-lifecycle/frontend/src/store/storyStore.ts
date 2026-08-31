import { create } from 'zustand'
import type { PatrolSummary } from '../api/client'

export interface StorySummary {
  storyKey: string
  title: string
  currentStage: string
  status: string
  profile: string
  executionCount: number
  createdAt?: string
  updatedAt: string
  intakeState?: string | null
  sourceType?: string
  sourceId?: string
  tapdType?: string
  tapdStatus?: string
  tapdUrl?: string
  deadline?: string
  owner?: string
  priority?: string
  // 班车看板
  releaseTrain?: string | null
  lifecycleState?: string | null
  isTest?: boolean | null
  // 生产巡检摘要(卡片徽标;itemsCount=0 或无数据时 null → 不显示徽标)
  patrolSummary?: PatrolSummary | null
}

interface StoryStore {
  stories: StorySummary[]
  connected: boolean
  setStories: (stories: StorySummary[]) => void
  setConnected: (connected: boolean) => void
  updateStory: (key: string, patch: Partial<StorySummary>) => void
  removeStory: (key: string) => void
}

export const useStoryStore = create<StoryStore>((set) => ({
  stories: [],
  connected: false,
  setStories: (stories) => set({ stories }),
  setConnected: (connected) => set({ connected }),
  updateStory: (key, patch) =>
    set((state) => ({
      stories: state.stories.map((s) =>
        s.storyKey === key ? { ...s, ...patch } : s
      ),
    })),
  removeStory: (key) =>
    set((state) => ({
      stories: state.stories.filter((s) => s.storyKey !== key),
    })),
}))
