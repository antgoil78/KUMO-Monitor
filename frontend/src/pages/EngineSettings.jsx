import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'

export default function EngineSettings() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const requestId = useRef(0)
  const busy = useRef(false)

  const load = useCallback(async () => {
    if (busy.current) return
    const id = ++requestId.current
    setLoading(true)
    try {
      const next = await api.engineSettings()
      if (id !== requestId.current) return
      setData(next)
      setError('')
    } catch (err) {
      if (id !== requestId.current) return
      setData(null)
      setError(err.message || String(err))
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    let timer
    async function poll() {
      await load()
      if (active) timer = window.setTimeout(poll, 15000)
    }
    poll()
    return () => {
      active = false
      window.clearTimeout(timer)
      requestId.current += 1
    }
  }, [load])

  async function changeEngine(enabled) {
    if (busy.current) return
    busy.current = true
    const id = ++requestId.current
    setLoading(false)
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const next = await api.setEngineEnabled(enabled)
      if (id !== requestId.current) return
      setData(next)
      setNotice(`${next.source === 'mock' ? 'Demo: ' : ''}${enabled ? 'Resume' : 'Suspend'} command applied.`)
    } catch (err) {
      if (id !== requestId.current) return
      // A timeout can occur after Snowflake has already applied the command.
      // Require a fresh status before offering another action.
      setData(null)
      setError(err.message || String(err))
    } finally {
      busy.current = false
      if (id === requestId.current) setSaving(false)
    }
  }

  const engine = data?.engine
  const running = ['STARTED', 'RESUMED'].includes(engine?.state)
  const suspended = engine?.state === 'SUSPENDED'

  return (
    <div className="vision-card settings-engine-card" aria-busy={loading || saving}>
      <div className="card-title-row">
        <div><h3>Workflow engine (ENGINE)</h3><span>Master task that dispatches queued workflow runs</span></div>
        <StatusBadge status={loading && !engine ? 'LOADING' : engine?.status || 'UNKNOWN'} />
      </div>

      {error && <div className="alert error" role="alert">{error}</div>}
      {notice && <div className="alert success" role="status">{notice}</div>}
      {data?.source === 'mock' && <p className="settings-engine-note">Demo mode: these controls update simulated engine state.</p>}

      <dl className="settings-engine-details">
        <div><dt>Master task</dt><dd><code>{engine?.taskFqn || 'Unavailable'}</code></dd></div>
        <div><dt>Snowflake state</dt><dd>{engine?.state || 'Unknown'}</dd></div>
      </dl>

      <p className="settings-engine-note">Resume enables dispatching queued workflow runs, including runs already waiting. Suspend pauses new dispatches; work already running can finish. Individual workflow schedules are managed separately.</p>
      {engine?.status === 'MISSING' && <p className="settings-engine-note">The master task was not found or is not visible to the application role in this environment.</p>}

      <div className="settings-engine-actions">
        <button type="button" className="button primary" disabled={loading || saving || !suspended} onClick={() => changeEngine(true)}>{saving && suspended ? 'Resuming…' : 'Resume engine'}</button>
        <button type="button" className="button danger" disabled={loading || saving || !running} onClick={() => changeEngine(false)}>{saving && running ? 'Suspending…' : 'Suspend engine'}</button>
        <button type="button" className="button" disabled={loading || saving} onClick={load}>{loading ? 'Refreshing…' : '↻ Refresh status'}</button>
      </div>
    </div>
  )
}
