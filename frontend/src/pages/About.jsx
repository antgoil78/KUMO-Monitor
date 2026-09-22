import { useEffect, useMemo, useState } from 'react'

import PageHeader from '../components/PageHeader.jsx'
import { api } from '../api.js'

function formatStartedAt(value) {
  if (!value) return 'Unknown'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' })
}

function formatUptime(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0))
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days) return `${days}d ${hours}h ${minutes}m`
  if (hours) return `${hours}h ${minutes}m`
  return `${minutes}m ${seconds % 60}s`
}

function formatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes)) return 'Unavailable'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let size = bytes / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024
    unit = units[index]
  }
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${unit}`
}

export default function About({ buildInfo: initialBuildInfo }) {
  const [buildInfo, setBuildInfo] = useState(initialBuildInfo)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    let cancelled = false
    const loadHealth = () => api.health().then(data => !cancelled && setBuildInfo(data)).catch(() => {})
    loadHealth()
    const clockId = window.setInterval(() => setNow(Date.now()), 1000)
    const healthId = window.setInterval(loadHealth, 15000)
    return () => {
      cancelled = true
      window.clearInterval(clockId)
      window.clearInterval(healthId)
    }
  }, [])

  const uptimeSeconds = useMemo(() => {
    const started = Date.parse(buildInfo?.startedAt || '')
    return Number.isFinite(started) ? Math.max(0, (now - started) / 1000) : buildInfo?.uptimeSeconds
  }, [buildInfo, now])
  const memory = buildInfo?.processMemory || {}
  const monitorCache = buildInfo?.backendCaches?.monitor || {}
  const dashboardCache = buildInfo?.backendCaches?.dashboard || {}

  return (
    <section className="page about-page">
      <PageHeader breadcrumb="KUMO Monitor / About" title="About" subtitle="Application build and runtime information." />

      <div className="about-hero">
        <img className="about-logo" src="/brand/kumo-monitor-logotype.png" alt="KUMO Monitor" />
      </div>

      <div className="about-info-grid">
        <div className="about-info-card"><span>Image version</span><strong>{buildInfo?.imageVersion || 'latest'}</strong><small>Docker image tag used for this deployment</small></div>
        <div className="about-info-card"><span>Build SHA</span><strong>{buildInfo?.buildSha || 'local'}</strong><small>Source revision supplied during the image build</small></div>
        <div className="about-info-card"><span>Runtime status</span><strong className="about-status"><i /> Running</strong><small>Backend API is responding</small></div>
        <div className="about-info-card"><span>Container started</span><strong>{formatStartedAt(buildInfo?.startedAt)}</strong><small>Backend process start time</small></div>
        <div className="about-info-card"><span>Current uptime</span><strong>{formatUptime(uptimeSeconds)}</strong><small>Time since the backend process started</small></div>
        <div className="about-info-card"><span>Container ID</span><strong title={buildInfo?.containerId}>{buildInfo?.containerId || 'Unknown'}</strong><small>Docker hostname for this running container</small></div>
        <div className="about-info-card"><span>Database context</span><strong>{buildInfo?.db || 'Unknown'}</strong><small>{buildInfo?.schema || 'Unknown schema'}</small></div>
        <div className="about-info-card"><span>Connection mode</span><strong>{buildInfo?.snowflakeConnectionMode || 'Unknown'}</strong><small>Snowflake authentication mode</small></div>
        <div className="about-info-card"><span>Runtime ID</span><strong title={buildInfo?.runtimeId}>{buildInfo?.runtimeId || 'Unknown'}</strong><small>Unique identifier for this backend process</small></div>
      </div>

      <div className="about-section-heading"><div><span>Backend diagnostics</span><h2>Memory &amp; cache</h2></div><small>Updates every 15 seconds without querying Snowflake</small></div>
      <div className="about-info-grid">
        <div className="about-info-card"><span>Process memory</span><strong>{formatBytes(memory.rssBytes)}</strong><small>Current resident memory · peak {formatBytes(memory.peakRssBytes)}</small></div>
        <div className="about-info-card"><span>Virtual memory</span><strong>{formatBytes(memory.virtualBytes)}</strong><small>Address space reserved by the backend process</small></div>
        <div className="about-info-card"><span>Monitor cache size</span><strong>{formatBytes(monitorCache.payloadBytes)}</strong><small>{monitorCache.workflowCount ?? 0} workflows · source {monitorCache.source || 'unknown'}</small></div>
        <div className="about-info-card"><span>Monitor cache refresh</span><strong>{monitorCache.refreshing ? 'Refreshing now' : `${monitorCache.refreshSeconds ?? '—'} seconds`}</strong><small>{monitorCache.threadAlive ? 'Thread running' : 'Thread stopped'} · {monitorCache.refreshCount ?? 0} completed refreshes</small></div>
        <div className="about-info-card"><span>Latest monitor refresh</span><strong>{formatStartedAt(monitorCache.lastRefreshAt)}</strong><small>{monitorCache.lastDurationMs == null ? 'No duration recorded' : `${monitorCache.lastDurationMs} ms duration`} · age {monitorCache.lastRefreshAgeSeconds ?? '—'}s</small></div>
        <div className="about-info-card"><span>Dashboard cache size</span><strong>{formatBytes(dashboardCache.payloadBytes)}</strong><small>{dashboardCache.threadAlive ? 'Thread running' : 'Thread stopped'} · {dashboardCache.refreshCount ?? 0} completed refreshes</small></div>
        <div className="about-info-card"><span>Dashboard refresh</span><strong>{dashboardCache.refreshing ? 'Refreshing now' : `${dashboardCache.refreshSeconds ?? '—'} seconds`}</strong><small>{dashboardCache.lastDurationMs == null ? 'No duration recorded' : `${dashboardCache.lastDurationMs} ms latest duration`}</small></div>
        <div className="about-info-card"><span>Cache health</span><strong className={monitorCache.lastError || dashboardCache.lastError ? 'about-cache-error' : 'about-cache-ok'}>{monitorCache.lastError || dashboardCache.lastError ? 'Attention required' : 'Healthy'}</strong><small>{monitorCache.lastError || dashboardCache.lastError || 'No cache errors reported'}</small></div>
      </div>
    </section>
  )
}
