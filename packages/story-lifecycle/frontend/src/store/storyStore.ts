import { create } from 'zustand'

export interface StorySummary {
  storyKey: string
  title: string
  currentStage: string
  status: string
  profile: string
  executionCount: number
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
