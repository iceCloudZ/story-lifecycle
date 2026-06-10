import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useStoryWebSocket } from './hooks/useWebSocket'
import Dashboard from './pages/Dashboard'
import StoryDetailPage from './pages/StoryDetailPage'
import QualityDashboard from './pages/QualityDashboard'
import DiagnosticsPage from './pages/DiagnosticsPage'
import UserDetailPage from './pages/UserDetailPage'
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
        </nav>
      </header>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/story/:key" element={<StoryDetailPage />} />
          <Route path="/quality" element={<QualityDashboard />} />
          <Route path="/diagnostics" element={<DiagnosticsPage />} />
          <Route path="/contact/:contactId" element={<UserDetailPage />} />
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
