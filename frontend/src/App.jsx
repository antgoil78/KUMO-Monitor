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
import Dependencies from './pages/Dependencies.jsx'
import EnvironmentSettings from './pages/EnvironmentSettings.jsx'

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
  settings: Settings,
  dependencies: Dependencies,
  environmentSettings: EnvironmentSettings
}

export default function App() {
  const query = new URLSearchParams(window.location.search)
  const astelPreview = query.get('preview') === 'astel'
  // The Corona-inspired design is now the application default. Keep the old
  // Astel query switch temporarily as an internal comparison/rollback aid.
  const coronaPreview = !astelPreview
  const stylePreview = astelPreview || coronaPreview
  const requestedPage = query.get('page')
  const [page, setPage] = useState(pages[requestedPage] ? requestedPage : 'dashboard')
  const [pageContext, setPageContext] = useState({})
  const [topbarSession, setTopbarSession] = useState(null)
  const Page = pages[page] || Dashboard

  // Keep one presence connection open for the lifetime of the application,
  // including pages that do not otherwise consume realtime status events.
  useEffect(() => {
    let cancelled = false
    const source = createKumoEventSource((event) => {
      window.dispatchEvent(new CustomEvent('kumo:realtime', { detail: event }))
    }, () => {}, { page })
    api.session().catch(() => null).then(sessionData => {
      if (!cancelled && sessionData) setTopbarSession(sessionData)
    })
    return () => { cancelled = true; source?.close() }
  }, [page])

  useEffect(() => {
    const renew = () => api.activity().catch(() => {})
    renew()
    const id = window.setInterval(renew, 30000)
    const onVisible = () => {
      if (document.visibilityState === 'visible') renew()
    }
    const onFocus = renew
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onFocus)
    }
  }, [])

  function navigate(nextPage, context = {}) {
    setPageContext(context)
    setPage(nextPage)
    if (stylePreview) {
      const url = new URL(window.location.href)
      url.searchParams.set('page', nextPage)
      window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
    }
  }

  function closeStylePreview() {
    const url = new URL(window.location.href)
    url.searchParams.delete('preview')
    url.searchParams.delete('page')
    window.location.assign(`${url.pathname}${url.search}${url.hash}`)
  }

  return (
    <div className={`app-shell ${astelPreview ? 'astel-preview' : ''} ${coronaPreview ? 'corona-preview' : ''}`}>
      <Sidebar activePage={page} onNavigate={navigate} session={topbarSession} />
      <main className="main-content">
        {astelPreview && (
          <div className="astel-topbar">
            <button type="button" className="astel-collapse" aria-label="Collapse sidebar">«</button>
            <label className="astel-search"><span>⌕</span><input placeholder="Search workflows, runs and settings" /></label>
            <div className="astel-top-actions">
              <button type="button" aria-label="Theme">☼</button>
              <button type="button" aria-label="Notifications">♢</button>
              <button type="button" className="astel-exit" onClick={closeStylePreview}>Exit preview</button>
            </div>
          </div>
        )}
        {coronaPreview && (
          <div className="corona-topbar">
            <button type="button" className="corona-menu" aria-label="Open environment and system settings" title="Environment and system settings" onClick={() => navigate('environmentSettings')}>☰</button>
            <div className="corona-top-actions">
              <span className="corona-live"><i /> Live</span>
              <button type="button" aria-label="Notifications">♢<i /></button>
            </div>
          </div>
        )}
        <Page {...pageContext} onNavigate={navigate} />
      </main>
    </div>
  )
}
