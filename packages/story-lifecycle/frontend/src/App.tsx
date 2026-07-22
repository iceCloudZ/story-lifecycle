import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useStoryWebSocket } from './hooks/useWebSocket'
import MoreMenu from './components/MoreMenu'
import Dashboard from './pages/Dashboard'
import StoryDetailPage from './pages/StoryDetailPage'
import DevPage from './pages/lifecycle/DevPage'
import TestReleasePage from './pages/lifecycle/TestReleasePage'
import DonePage from './pages/lifecycle/DonePage'
import QualityDashboard from './pages/QualityDashboard'
import DiagnosticsPage from './pages/DiagnosticsPage'
import BugsPage from './pages/BugsPage'
import DocSearchPage from './pages/DocSearchPage'
import TapdBoardPage from './pages/TapdBoardPage'
import CalendarPage from './pages/CalendarPage'
import ProjectsPage from './pages/ProjectsPage'
import ReleaseTrainBoard from './pages/ReleaseTrainBoard'
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
          <MoreMenu
            sections={[
              {
                label: '视图',
                items: [
                  { to: '/tapd', text: 'TAPD 需求' },
                  { to: '/calendar', text: '日历' },
                  { to: '/projects', text: '项目' },
                  { to: '/release-train', text: '班车看板' },
                ],
              },
              {
                label: '横切',
                items: [
                  { to: '/bugs', text: '缺陷' },
                  { to: '/quality', text: '质量' },
                  { to: '/diagnostics', text: '诊断' },
                  { to: '/docs/search', text: '文档搜索' },
                ],
              },
            ]}
          />
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
          <Route path="/tapd" element={<TapdBoardPage />} />
          <Route path="/calendar" element={<CalendarPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/release-train" element={<ReleaseTrainBoard />} />
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
