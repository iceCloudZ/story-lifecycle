import { useState } from 'react'
import { useStories } from './hooks/useWebSocket'
import StoryList from './components/StoryList'
import StoryDetail from './components/StoryDetail'
import './App.css'

function App() {
  const { stories, connected } = useStories()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  return (
    <div className="app">
      <header className="header">
        <h1>Story Lifecycle</h1>
        <div className="header-status">
          <span className={`ws-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span>{connected ? '已连接' : '断开连接'}</span>
          <span className="story-count">{stories.length} 个 Story</span>
        </div>
      </header>
      <main className="main">
        <StoryList
          stories={stories}
          selectedKey={selectedKey}
          onSelect={setSelectedKey}
        />
        <aside className="right-panel">
          {selectedKey ? (
            <StoryDetail key={selectedKey} storyKey={selectedKey} />
          ) : (
            <div className="no-selection">选择一个 Story 查看详情</div>
          )}
        </aside>
      </main>
    </div>
  )
}

export default App
