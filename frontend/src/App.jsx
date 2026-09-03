import { useEffect, useState } from 'react'

import Sidebar from './components/Sidebar.jsx'
import { api, createKumoEventSource } from './api.js'
import Dashboard from './pages/Dashboard.jsx'
import Monitor from './pages/Monitor.jsx'
import History from './pages/History.jsx'
import Notifications from './pages/Notifications.jsx'
import FileIngestion, { FileIngestionDetail } from './pages/FileIngestion.jsx'
import LimReload from './pages/LimReload.jsx'
import DagView from './pages/DagView.jsx'
import ExecutionLog from './pages/ExecutionLog.jsx'
import Admin from './pages/Admin.jsx'
import Settings from './pages/Settings.jsx'

const pages = {
  dashboard: Dashboard,
  monitor: Monitor,
  history: History,
  notifications: Notifications,
  fileIngestion: FileIngestion,
  fileIngestionDetail: FileIngestionDetail,
  limReload: LimReload,
  dag: DagView,
  executionLog: ExecutionLog,
  admin: Admin,
  settings: Settings
}

export default function App() {
  const [page, setPage] = useState('dashboard')
  const [pageContext, setPageContext] = useState({})
  const Page = pages[page] || Dashboard

  // Keep one presence connection open for the lifetime of the application,
  // including pages that do not otherwise consume realtime status events.
  useEffect(() => {
    let cancelled = false
    let source = null
    api.session().catch(() => null).then(() => {
      if (cancelled) return
      source = createKumoEventSource((event) => {
        window.dispatchEvent(new CustomEvent('kumo:realtime', { detail: event }))
      }, () => {}, { page })
    })
    return () => { cancelled = true; source?.close() }
  }, [page])

  useEffect(() => {
    const renew = () => api.activity().catch(() => {})
    let id = null
    let ready = false
    let cancelled = false
    api.session().catch(() => null).then(() => {
      if (cancelled) return
      ready = true
      renew()
      id = window.setInterval(renew, 30000)
    })
    const onVisible = () => {
      if (ready && document.visibilityState === 'visible') renew()
    }
    const onFocus = () => { if (ready) renew() }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onFocus)
    return () => {
      cancelled = true
      if (id) window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onFocus)
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
