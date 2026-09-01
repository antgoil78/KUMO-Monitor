import { useEffect, useState } from 'react'

import Sidebar from './components/Sidebar.jsx'
import { api, createKumoEventSource } from './api.js'
import Dashboard from './pages/Dashboard.jsx'
import Monitor from './pages/Monitor.jsx'
import History from './pages/History.jsx'
import Notifications from './pages/Notifications.jsx'
import FileIngestion from './pages/FileIngestion.jsx'
import LimReload from './pages/LimReload.jsx'
import DagView from './pages/DagView.jsx'
import ExecutionLog from './pages/ExecutionLog.jsx'

const pages = {
  dashboard: Dashboard,
  monitor: Monitor,
  history: History,
  notifications: Notifications,
  fileIngestion: FileIngestion,
  limReload: LimReload,
  dag: DagView,
  executionLog: ExecutionLog
}

export default function App() {
  const [page, setPage] = useState('dashboard')
  const [pageContext, setPageContext] = useState({})
  const Page = pages[page] || Dashboard

  // Keep one presence connection open for the lifetime of the application,
  // including pages that do not otherwise consume realtime status events.
  useEffect(() => {
    const source = createKumoEventSource((event) => {
      window.dispatchEvent(new CustomEvent('kumo:realtime', { detail: event }))
    }, () => {}, { page: 'Application' })
    return () => source?.close()
  }, [])

  useEffect(() => {
    const renew = () => api.activity().catch(() => {})
    renew()
    const id = window.setInterval(renew, 30000)
    const onVisible = () => {
      if (document.visibilityState === 'visible') renew()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', renew)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', renew)
    }
  }, [])

  function navigate(nextPage, context = {}) {
    setPageContext(context)
    setPage(nextPage)
  }

  return (
    <div className="app-shell">
      <Sidebar activePage={page} onNavigate={navigate} />
      <main className="main-content">
        <Page {...pageContext} onNavigate={navigate} />
      </main>
    </div>
  )
}
