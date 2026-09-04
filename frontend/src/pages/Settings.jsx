import { useEffect, useState } from 'react'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import { formatDateTimeSeconds } from '../utils/time.js'

const backendIntervals = [5, 10, 30, 60, 120, 300]

function StatusItem({ label, value, ok = true, detail }) {
  return (
    <div className="admin-status-item">
      <span className={`health-led ${ok ? 'success' : 'failed'}`} />
      <div><strong>{label}</strong><span>{value}</span>{detail && <small>{detail}</small>}</div>
    </div>
  )
}

export default function Settings() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [nowMs, setNowMs] = useState(Date.now())

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
    const id = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    function onRealtime(browserEvent) {
      const event = browserEvent.detail
      if (event?.type === 'realtime_state') {
        setData(previous => previous ? { ...previous, realtime: event.data } : previous)
      }
      if (event?.type === 'monitor_refresh_started' || event?.type === 'monitor_refresh_completed') {
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
    setSaving(true)
    setError(null)
    try {
      const settings = await api.updateBackendRefresh(Number(value))
      setData(previous => previous ? { ...previous, settings } : previous)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setSaving(false)
    }
  }

  const monitor = data?.realtime?.monitorCache || {}
  const refreshSeconds = Number(data?.settings?.backendCacheRefreshSeconds || monitor.refreshSeconds || 30)
  const lastRefreshMs = Date.parse(monitor.lastRefreshAt || '')
  const ageSeconds = Number.isFinite(lastRefreshMs) ? Math.max(0, Math.floor((nowMs - lastRefreshMs) / 1000)) : null
  const phases = monitor.lastPhaseMs || {}

  return (
    <section className="page admin-page">
      <PageHeader breadcrumb="Application Administration / Settings" title="Settings" subtitle="Configure the shared backend cache refresh cycle." actions={<button type="button" className="button" onClick={load}>↻ Refresh status</button>} />

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
    </section>
  )
}
