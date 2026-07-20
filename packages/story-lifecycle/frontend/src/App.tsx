import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useStoryWebSocket } from './hooks/useWebSocket'
import Dashboard from './pages/Dashboard'
import StoryDetailPage from './pages/StoryDetailPage'
import DevPage from './pages/lifecycle/DevPage'
import TestReleasePage from './pages/lifecycle/TestReleasePage'
import DonePage from './pages/lifecycle/DonePage'
import QualityDashboard from './pages/QualityDashboard'
import DiagnosticsPage from './pages/DiagnosticsPage'
import BugsPage from './pages/BugsPage'
import DocSearchPage from './pages/DocSearchPage'
import './App.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      retry: 1,
    },
  },
})

function AppContent() {
  useStoryWebSocket()

  return (
    <div className="app">
      <header className="header">
        <h1 className="header-title">Story Lifecycle</h1>
        <nav className="header-nav">
          {/* 生命周期段:intake → 开发 → 测试上线 → 结项,用户秒懂"在管哪一阶段" */}
          <span className="nav-group-label">生命周期</span>
          <NavLink to="/" end className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            待启动
          </NavLink>
          <NavLink to="/dev" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            开发中
          </NavLink>
          <NavLink to="/test-release" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            测试·上线
          </NavLink>
          <NavLink to="/done" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            已结项
          </NavLink>
          <span className="nav-divider" />
          {/* 横切视图:与单个 story 的生命周期正交 */}
          <span className="nav-group-label">横切</span>
          <NavLink to="/bugs" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            缺陷
          </NavLink>
          <NavLink to="/quality" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            质量
          </NavLink>
          <NavLink to="/diagnostics" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            诊断
          </NavLink>
          <NavLink to="/docs/search" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            文档搜索
          </NavLink>
        </nav>
      </header>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/story/:key" element={<StoryDetailPage />} />
          <Route path="/dev" element={<DevPage />} />
          <Route path="/test-release" element={<TestReleasePage />} />
          <Route path="/done" element={<DonePage />} />
          <Route path="/quality" element={<QualityDashboard />} />
          <Route path="/diagnostics" element={<DiagnosticsPage />} />
          <Route path="/bugs" element={<BugsPage />} />
          <Route path="/docs/search" element={<DocSearchPage />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppContent />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
