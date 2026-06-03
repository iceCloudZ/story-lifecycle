import { useState } from 'react'
import { useStories } from './hooks/useWebSocket'
import StoryList from './components/StoryList'
import StoryDetail from './components/StoryDetail'
import TerminalPanel from './components/TerminalPanel'
import './App.css'

type Tab = 'detail' | 'terminal'

function App() {
  const { stories, connected } = useStories()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('detail')

  function handleSelect(key: string) {
    setSelectedKey(key)
    setTab('detail')
  }

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
          onSelect={handleSelect}
        />
        <aside className="right-panel">
          {selectedKey && (
            <div className="panel-tabs">
              <button
                className={`tab ${tab === 'detail' ? 'active' : ''}`}
                onClick={() => setTab('detail')}
              >
                详情
              </button>
              <button
                className={`tab ${tab === 'terminal' ? 'active' : ''}`}
                onClick={() => setTab('terminal')}
              >
                终端
              </button>
            </div>
          )}
          <div className="panel-content">
            {tab === 'detail' && selectedKey && (
              <StoryDetail key={selectedKey} storyKey={selectedKey} />
            )}
            {tab === 'terminal' && (
              <TerminalPanel storyKey={selectedKey} />
            )}
            {!selectedKey && (
              <div className="no-selection">选择一个 Story 查看详情</div>
            )}
          </div>
        </aside>
      </main>
    </div>
  )
}

export default App
