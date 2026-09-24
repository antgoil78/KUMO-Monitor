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
import About from './pages/About.jsx'

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
  environmentSettings: EnvironmentSettings,
  about: About
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
  const [identityReady, setIdentityReady] = useState(false)
  const [buildInfo, setBuildInfo] = useState(null)
  const [realtimeConnected, setRealtimeConnected] = useState(false)
  const [snowflakeStatus, setSnowflakeStatus] = useState(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.localStorage.getItem('kumoSidebarCollapsed') === 'true')
  const Page = pages[page] || Dashboard

  // Keep one presence connection open for the lifetime of the application,
  // including pages that do not otherwise consume realtime status events.
  useEffect(() => {
    let cancelled = false
    let source = null
    setRealtimeConnected(false)
    // Register this browser's current Snowflake identity before opening the
    // presence stream, so run requests cannot inherit an older client actor.
    api.session().catch(() => null).then(sessionData => {
      if (cancelled) return
      if (sessionData) setTopbarSession(sessionData)
      setIdentityReady(true)
      source = createKumoEventSource((event) => {
        if (event?.type === 'connected' || event?.type === 'heartbeat') setRealtimeConnected(true)
        window.dispatchEvent(new CustomEvent('kumo:realtime', { detail: event }))
      }, () => setRealtimeConnected(false), { page })
    })
    api.health().catch(() => null).then(healthData => {
      if (!cancelled && healthData) setBuildInfo(healthData)
    })
    return () => { cancelled = true; source?.close() }
  }, [page])

  useEffect(() => {
    if (!identityReady) return undefined
    let cancelled = false
    const loadSnowflakeStatus = () => api.snowflakePing()
      .then(data => { if (!cancelled) setSnowflakeStatus(data) })
      .catch(error => { if (!cancelled) setSnowflakeStatus({ ok: false, error: error.message }) })
    loadSnowflakeStatus()
    const id = window.setInterval(loadSnowflakeStatus, 10000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [identityReady])

  useEffect(() => {
    if (!identityReady) return undefined
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
  }, [identityReady])

  function navigate(nextPage, context = {}) {
    setPageContext(context)
    setPage(nextPage)
    if (stylePreview) {
      const url = new URL(window.location.href)
      url.searchParams.set('page', nextPage)
      window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
    }
  }

  function toggleSidebar() {
    setSidebarCollapsed(current => {
      const next = !current
      window.localStorage.setItem('kumoSidebarCollapsed', String(next))
      return next
    })
  }

  function logout() {
    window.location.assign('/sfc-endpoint/logout')
  }

  function closeStylePreview() {
    const url = new URL(window.location.href)
    url.searchParams.delete('preview')
    url.searchParams.delete('page')
    window.location.assign(`${url.pathname}${url.search}${url.hash}`)
  }

  return (
    <div className={`app-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''} ${astelPreview ? 'astel-preview' : ''} ${coronaPreview ? 'corona-preview' : ''}`}>
      <Sidebar activePage={page} onNavigate={navigate} session={topbarSession} buildInfo={buildInfo} collapsed={sidebarCollapsed} />
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
            <button type="button" className="corona-menu" aria-label={sidebarCollapsed ? 'Expand side menu' : 'Collapse side menu'} title={sidebarCollapsed ? 'Expand side menu' : 'Collapse side menu'} aria-expanded={!sidebarCollapsed} onClick={toggleSidebar}>☰</button>
            <div className="corona-top-actions">
              <span className={`corona-connection ${realtimeConnected ? 'connected' : 'disconnected'}`} title="Live server-to-browser event stream"><i /> Realtime {realtimeConnected ? 'connected' : 'reconnecting'}</span>
              <span className={`corona-connection ${snowflakeStatus?.ok ? 'connected' : 'disconnected'}`} title={snowflakeStatus?.error || `Snowflake ${snowflakeStatus?.mode || 'connection'}`}><i /> Snowflake {snowflakeStatus?.ok ? 'connected' : 'unavailable'}</span>
              <button type="button" className={`corona-settings-button ${page === 'settings' ? 'active' : ''}`} aria-label="Open settings" title="Settings" onClick={() => navigate('settings')}><span aria-hidden="true">⚙</span></button>
              <button type="button" className="corona-logout-button" aria-label="Log out of KUMO Monitor" title="Log out" onClick={logout}>
                <svg aria-hidden="true" viewBox="0 0 24 24" focusable="false"><path d="M10 5H5v14h5M14 8l4 4-4 4M8 12h10" /></svg>
                <span>Log out</span>
              </button>
            </div>
          </div>
        )}
        {identityReady
          ? <Page {...pageContext} onNavigate={navigate} buildInfo={buildInfo} />
          : <div className="loading-state">Resolving Snowflake user…</div>}
      </main>
    </div>
  )
}
