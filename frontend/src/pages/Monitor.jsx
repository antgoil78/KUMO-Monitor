import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import StatusBadge, { statusKind } from '../components/StatusBadge.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'

const statusOptions = ['SUCCESS', 'FAILED', 'RUNNING', 'QUEUED', '-']

function KpiCard({ label, value, tone }) {
  return (
    <div className={`kpi-card ${tone || ''}`}>
      <div className="kpi-value">{value}</div>
      <div className="kpi-label">{label}</div>
    </div>
  )
}

function WorkflowRow({ workflow, nowMs, onRun }) {
  const disabled = !workflow.workflowEnabled
  return (
    <tr className={disabled ? 'disabled-row' : ''}>
      <td className="workflow-cell">
        <div className="workflow-name" style={{ paddingLeft: `${(workflow.indent || 0) * 24}px` }}>
          {workflow.indent > 0 && <span className="child-arrow">↳</span>}
          <span>{workflow.workflowName}</span>
          <span className="type-chip">{workflow.workflowType || 'DBT'}</span>
        </div>
        <div className="workflow-meta">
          {workflow.workflowGroup || 'Ungrouped'}
          {workflow.lastRunId ? <span>Run ID: {workflow.lastRunId}</span> : null}
        </div>
      </td>
      <td><StatusBadge status={workflow.lastStatus} /></td>
      <td>{formatDateTime(workflow.lastStartTime)}</td>
      <td>{elapsedDuration(workflow.lastStartTime, workflow.lastEndTime, workflow.lastStatus, nowMs)}</td>
      <td className="schedule-cell">
        {workflow.taskEnabled ? (
          <>
            <code>{workflow.scheduleCron || '-'}</code>
            <span>{workflow.scheduleTimezone || 'UTC'}</span>
          </>
        ) : '-'}
      </td>
      <td>{workflow.taskEnabled ? formatDateTime(workflow.nextRunTime) : '-'}</td>
      <td><ProgressBar progress={workflow.progress} status={workflow.lastStatus} /></td>
      <td className="row-actions">
        <button className="small-button primary" disabled={disabled} onClick={() => onRun(workflow)}>Run</button>
      </td>
    </tr>
  )
}

export default function Monitor() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [statuses, setStatuses] = useState([])
  const [nowMs, setNowMs] = useState(Date.now())
  const [actionMessage, setActionMessage] = useState(null)

  async function load(force = false) {
    try {
      setError(null)
      const data = force ? await api.refreshMonitor() : await api.monitor()
      setPayload(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(false)
  }, [])

  useEffect(() => {
    const intervalMs = payload?.refreshIntervalMs || 5000
    const id = setInterval(() => load(false), intervalMs)
    return () => clearInterval(id)
  }, [payload?.refreshIntervalMs])

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const workflows = payload?.workflows || []
  const summary = payload?.summary || { total: 0, success: 0, failed: 0, running: 0, queued: 0 }
  const engine = payload?.engine || { status: 'UNKNOWN' }

  const filtered = useMemo(() => {
    const f = filter.trim().toLowerCase()
    return workflows.filter(w => {
      const matchesName = !f || [w.workflowName, w.workflowGroup, w.workflowType, w.lastRunId].some(v => String(v || '').toLowerCase().includes(f))
      const st = String(w.lastStatus || '-').toUpperCase()
      const matchesStatus = statuses.length === 0 || statuses.includes(st)
      return matchesName && matchesStatus
    })
  }, [workflows, filter, statuses])

  async function runWorkflow(workflow) {
    setActionMessage(null)
    try {
      const result = await api.runWorkflow(workflow.workflowId)
      setActionMessage(`Triggered ${workflow.workflowName}. Run ID: ${result.runId}`)
      await load(true)
    } catch (err) {
      setActionMessage(`Failed to trigger ${workflow.workflowName}: ${err.message}`)
    }
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <p className="eyebrow">KUMO Monitor</p>
          <h1>Monitor</h1>
          <p className="page-subtitle">Latest and current workflow status. Data refreshes automatically.</p>
        </div>
        <div className="header-actions">
          <div className={`engine-pill ${statusKind(engine.status)}`}>
            <span className="pulse-dot" />
            Engine: <strong>{engine.status || 'UNKNOWN'}</strong>
          </div>
          <button className="button" onClick={() => load(true)}>Refresh now</button>
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}
      {actionMessage && <div className="alert info">{actionMessage}</div>}

      <div className="kpi-grid">
        <KpiCard label="Total" value={summary.total} />
        <KpiCard label="Success" value={summary.success} tone="success" />
        <KpiCard label="Failed" value={summary.failed} tone="failed" />
        <KpiCard label="Running" value={summary.running} tone="running" />
        <KpiCard label="Queued" value={summary.queued} tone="queued" />
      </div>

      <div className="toolbar">
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter workflows..."
          className="search-input"
        />
        <div className="status-filter">
          {statusOptions.map(st => (
            <button
              key={st}
              className={`filter-chip ${statuses.includes(st) ? 'active' : ''}`}
              onClick={() => setStatuses(prev => prev.includes(st) ? prev.filter(x => x !== st) : [...prev, st])}
            >
              {st}
            </button>
          ))}
        </div>
        <span className="updated-at">Updated: {formatDateTime(payload?.generatedAt)}</span>
      </div>

      <div className="table-card">
        {loading ? <div className="empty-state">Loading monitor data...</div> : null}
        {!loading && filtered.length === 0 ? <div className="empty-state">No workflows match the current filters.</div> : null}
        {filtered.length > 0 && (
          <table className="workflow-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Status</th>
                <th>Last run</th>
                <th>Duration</th>
                <th>Schedule</th>
                <th>Next run</th>
                <th>Progress</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(w => <WorkflowRow key={`${w.workflowId}-${w.lastRunId || 'none'}`} workflow={w} nowMs={nowMs} onRun={runWorkflow} />)}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
