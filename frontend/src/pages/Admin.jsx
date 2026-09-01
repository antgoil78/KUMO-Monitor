import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import { formatDateTimeSeconds } from '../utils/time.js'

const backendIntervals = [5, 10, 30, 60, 120, 300]

function formatLogTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '--:--:--.---'
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3
  }).format(date)
}

function StatusItem({ label, value, ok = true, detail }) {
  return (
    <div className="admin-status-item">
      <span className={`health-led ${ok ? 'success' : 'failed'}`} />
      <div><strong>{label}</strong><span>{value}</span>{detail && <small>{detail}</small>}</div>
    </div>
  )
}

export default function Admin() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [nowMs, setNowMs] = useState(Date.now())
  const [activities, setActivities] = useState([])
  const [activityFilter, setActivityFilter] = useState('ALL')
  const [activityPaused, setActivityPaused] = useState(false)

  async function load() {
    try {
      const next = await api.dashboard()
      setData(next)
      setError(next.cache?.dashboardError || null)
    } catch (err) {
      setError(err.message || String(err))
    }
  }

  useEffect(() => {
    load()
    const id = window.setInterval(load, 5000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    if (activityPaused) return undefined
    let cancelled = false
    const loadActivities = () => api.adminActivityLog(750).then(result => {
      if (!cancelled) setActivities(result.activities || [])
    }).catch(err => {
      if (!cancelled) setError(err.message || String(err))
    })
    loadActivities()
    const id = window.setInterval(loadActivities, 2000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [activityPaused])

  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    function onRealtime(browserEvent) {
      const event = browserEvent.detail
      if (event?.type === 'realtime_state') {
        setData(previous => previous ? { ...previous, realtime: event.data } : previous)
      }
      if (event?.type === 'monitor_refresh_started') {
        setData(previous => previous ? {
          ...previous,
          realtime: { ...(previous.realtime || {}), monitorCache: event.data }
        } : previous)
      }
      if (event?.type === 'monitor_refresh_completed') {
        setData(previous => previous ? {
          ...previous,
          realtime: { ...(previous.realtime || {}), monitorCache: event.data }
        } : previous)
      }
    }
    window.addEventListener('kumo:realtime', onRealtime)
    return () => window.removeEventListener('kumo:realtime', onRealtime)
  }, [])

  async function changeBackendInterval(value) {
    const seconds = Number(value)
    setSaving(true)
    setError(null)
    try {
      const settings = await api.updateBackendRefresh(seconds)
      setData(previous => previous ? { ...previous, settings } : previous)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setSaving(false)
    }
  }

  const realtime = data?.realtime || {}
  const monitor = realtime.monitorCache || {}
  const refreshSeconds = Number(data?.settings?.backendCacheRefreshSeconds || monitor.refreshSeconds || 30)
  const lastRefreshMs = Date.parse(monitor.lastRefreshAt || '')
  const ageSeconds = Number.isFinite(lastRefreshMs) ? Math.max(0, Math.floor((nowMs - lastRefreshMs) / 1000)) : null
  const phases = monitor.lastPhaseMs || {}
  const filteredActivities = useMemo(() => activityFilter === 'ALL' ? activities : activities.filter(item => item.category === activityFilter), [activities, activityFilter])

  return (
    <section className="page admin-page">
      <div className="page-header admin-header">
        <div><p className="breadcrumb">Pages / Admin</p><h1>Application Administration</h1><p>Shared Snowflake cache settings and live application activity.</p></div>
        <button type="button" className="button" onClick={load}>↻ Refresh status</button>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="vision-card admin-cache-card admin-cache-compact">
          <div className="card-title-row">
            <div><h3>Shared monitor cache</h3><span>One Snowflake refresh cycle shared by every client</span></div>
            <div className={`admin-cache-spinner ${monitor.refreshing ? 'active' : ''}`} aria-label={monitor.refreshing ? 'Refreshing from Snowflake' : 'Waiting'}>↻</div>
          </div>

          <label className="admin-refresh-setting">
            <span>Backend database refresh interval</span>
            <select value={refreshSeconds} disabled={saving} onChange={event => changeBackendInterval(event.target.value)}>
              {backendIntervals.map(seconds => <option key={seconds} value={seconds}>{seconds} seconds</option>)}
            </select>
            <small>This changes the single shared Snowflake-to-backend cycle for all clients. It resets after a service restart unless configured through KUMO_REFRESH_SECONDS.</small>
          </label>

          <div className="admin-status-list">
            <StatusItem label="Cache thread" value={monitor.threadAlive ? 'Alive' : 'Stopped'} ok={Boolean(monitor.threadAlive)} detail={monitor.refreshing ? 'Refreshing from Snowflake now' : `Configured every ${refreshSeconds}s`} />
            <StatusItem label="Last database refresh" value={formatDateTimeSeconds(monitor.lastRefreshAt)} ok={!monitor.lastError && Boolean(monitor.lastRefreshAt)} detail={ageSeconds == null ? 'No completed refresh' : `${ageSeconds} seconds old`} />
            <StatusItem label="Latest duration" value={monitor.lastDurationMs == null ? '—' : `${monitor.lastDurationMs}ms`} ok={!monitor.lastError} detail={`Connect ${phases.connection ?? '—'}ms · workflows ${phases.workflows ?? '—'}ms · engine ${phases.engine ?? '—'}ms`} />
            <StatusItem label="Latest error" value={monitor.lastError || 'None'} ok={!monitor.lastError} />
          </div>
      </div>

      <div className="vision-card admin-activity-card">
        <div className="card-title-row admin-log-title">
          <div><h3>Live application log</h3><span>User actions, application work, system activity, and every Snowflake call</span></div>
          <div className="admin-log-actions">
            <select value={activityFilter} onChange={event => setActivityFilter(event.target.value)}>
              <option value="ALL">All activity</option><option value="USER">User activity</option><option value="DATABASE">Database calls</option><option value="APPLICATION">Application</option><option value="SYSTEM">System</option>
            </select>
            <button type="button" className="button" onClick={() => setActivityPaused(value => !value)}>{activityPaused ? '▶ Resume' : 'Ⅱ Pause'}</button>
          </div>
        </div>
        <div className="admin-log-summary"><span>{filteredActivities.length} entries</span><span className="admin-log-live-dot" />{activityPaused ? 'Paused' : 'Live · updates every 2s'}</div>
        <div className="admin-terminal" role="log" aria-live="polite">
          <div className="admin-terminal-bar"><span className="terminal-dot red" /><span className="terminal-dot amber" /><span className="terminal-dot green" /><code>kumo-monitor --follow --newest-first</code></div>
          <div className="admin-terminal-output">
            {filteredActivities.length ? filteredActivities.map(item => {
              const details = item.details || {}
              const category = String(item.category || 'APPLICATION').toLowerCase()
              const status = String(item.status || 'SUCCESS').toLowerCase()
              const context = item.category === 'DATABASE'
                ? details.queryTag || 'SNOWFLAKE'
                : item.actor?.userName || item.actor?.displayName || 'BACKEND'
              const metadata = [details.queryId && `query=${details.queryId}`, details.httpStatus && `http=${details.httpStatus}`, details.ipAddress && `ip=${details.ipAddress}`].filter(Boolean).join(' ')
              return (
                <div className={`terminal-entry ${status}`} key={item.id}>
                  <div className="terminal-line">
                    <span className="terminal-time">{formatLogTime(item.startedAt)}</span>
                    <span className={`terminal-category ${category}`}>[{item.category}]</span>
                    <span className={`terminal-status ${status}`}>{item.status}</span>
                    <span className="terminal-duration">{item.durationMs == null ? '   —   ' : `${String(item.durationMs).padStart(5, ' ')}ms`}</span>
                    <span className="terminal-context">{context}</span>
                    <span className="terminal-message">{item.label}</span>
                    {metadata && <span className="terminal-metadata">{metadata}</span>}
                  </div>
                  {item.error && <div className="terminal-error">↳ {item.error}</div>}
                </div>
              )
            }) : <div className="terminal-empty">$ waiting for application activity…</div>}
          </div>
        </div>
      </div>
    </section>
  )
}
