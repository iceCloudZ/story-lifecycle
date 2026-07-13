import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useStoryWebSocket } from './hooks/useWebSocket'
import Dashboard from './pages/Dashboard'
import StoryDetailPage from './pages/StoryDetailPage'
import QualityDashboard from './pages/QualityDashboard'
import DiagnosticsPage from './pages/DiagnosticsPage'
import BugsPage from './pages/BugsPage'
import DiffPreviewPage from './pages/DiffPreviewPage'
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
            Dashboard
          </NavLink>
          <NavLink to="/quality" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            Quality
          </NavLink>
          <NavLink to="/diagnostics" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            Diagnostics
          </NavLink>
          <NavLink to="/bugs" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            缺陷
          </NavLink>
          <NavLink to="/release-train" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
            班车看板
          </NavLink>
        </nav>
      </header>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/story/:key" element={<StoryDetailPage />} />
          <Route path="/quality" element={<QualityDashboard />} />
          <Route path="/diagnostics" element={<DiagnosticsPage />} />
          <Route path="/bugs" element={<BugsPage />} />
          <Route path="/release-train" element={<ReleaseTrainBoard />} />
          <Route path="/diff-preview/:key" element={<DiffPreviewPage />} />
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
