import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: '2rem', color: '#e74c3c' }}>
          <h2>页面出错了</h2>
          <p>请尝试刷新页面，或联系管理员</p>
          <pre style={{ marginTop: '1rem', padding: '1rem', background: '#1a1a2e', borderRadius: 8, overflow: 'auto', maxHeight: 300 }}>
            {this.state.error?.message}
          </pre>
        </div>
      )
    }
    return this.props.children
  }
}
