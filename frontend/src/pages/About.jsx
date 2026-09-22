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

export default function About({ buildInfo: initialBuildInfo }) {
  const [buildInfo, setBuildInfo] = useState(initialBuildInfo)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    let cancelled = false
    api.health().then(data => !cancelled && setBuildInfo(data)).catch(() => {})
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [])

  const uptimeSeconds = useMemo(() => {
    const started = Date.parse(buildInfo?.startedAt || '')
    return Number.isFinite(started) ? Math.max(0, (now - started) / 1000) : buildInfo?.uptimeSeconds
  }, [buildInfo, now])

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
    </section>
  )
}
