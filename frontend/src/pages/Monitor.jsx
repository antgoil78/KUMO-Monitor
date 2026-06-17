import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import StatusBadge, { statusKind } from '../components/StatusBadge.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'

const statusOptions = [
  { value: '', label: 'All statuses' },
  { value: 'SUCCESS', label: 'Success' },
  { value: 'FAILED', label: 'Failed' },
  { value: 'RUNNING', label: 'Running' },
  { value: 'QUEUED', label: 'Queued' },
  { value: '-', label: 'No status' }
]

function SummaryItem({ tone, icon, label, value }) {
  return (
    <span className={`summary-item ${tone || ''}`}>
      <span className="summary-icon">{icon}</span>
      <strong>{value}</strong>
      <span>{label}</span>
    </span>
  )
}

function RunningProgress({ workflow }) {
  const s = String(workflow.lastStatus || '').toUpperCase()
  const isRunning = ['RUNNING', 'IN_PROGRESS', 'EXECUTING'].includes(s)
  const isQueued = ['QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED'].includes(s)

  if (!isRunning && !isQueued && !workflow.progress) return null

  return (
    <div className="inline-progress">
      <ProgressBar progress={workflow.progress} status={workflow.lastStatus} />
    </div>
  )
}

function WorkflowRow({ workflow, nowMs, onRun }) {
  const disabled = !workflow.workflowEnabled
  const depth = Number(workflow.indent || 0)
  const type = String(workflow.workflowType || 'DBT').toUpperCase()
  const isRoot = depth === 0

  return (
    <tr className={`${disabled ? 'disabled-row' : ''} ${isRoot ? 'root-row' : 'child-row'}`}>
      <td className="workflow-cell">
        <div className={`workflow-tree depth-${Math.min(depth, 6)}`} style={{ '--depth': depth }}>
          {depth > 0 && <span className="tree-branch" aria-hidden="true" />}
          <span className={`workflow-title ${isRoot ? 'root' : 'child'}`}>{workflow.workflowName}</span>
          <span className={`type-chip ${type.toLowerCase()}`}>{type}</span>
        </div>
      </td>
      <td className="status-cell">
        <StatusBadge status={workflow.lastStatus} />
      </td>
      <td className="muted-cell">{formatDateTime(workflow.lastStartTime)}</td>
      <td className="duration-cell">
        <span>{elapsedDuration(workflow.lastStartTime, workflow.lastEndTime, workflow.lastStatus, nowMs)}</span>
        <RunningProgress workflow={workflow} />
      </td>
      <td className="schedule-cell">
        {workflow.taskEnabled ? (
          <>
            <code>{workflow.scheduleCron || '-'}</code>
            <span>{workflow.scheduleTimezone || 'UTC'}</span>
          </>
        ) : <span className="muted-dash">—</span>}
      </td>
      <td className="muted-cell">{workflow.taskEnabled ? formatDateTime(workflow.nextRunTime) : '—'}</td>
      <td className="row-actions">
        <details className="row-menu">
          <summary aria-label={`Actions for ${workflow.workflowName}`}>⋮⌄</summary>
          <div className="row-menu-panel">
            <button disabled={disabled} onClick={() => onRun(workflow)}>Run workflow</button>
            <button disabled>History</button>
            <button disabled>Edit</button>
          </div>
        </details>
      </td>
    </tr>
  )
}

export default function Monitor() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
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
      const matchesStatus = !statusFilter || statusFilter === st
      return matchesName && matchesStatus
    })
  }, [workflows, filter, statusFilter])

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
    <section className="page monitor-page">
      <div className="monitor-heading">
        <h1>Workflow Monitor</h1>
        <button className="ghost-refresh" onClick={() => load(true)}>Refresh now</button>
      </div>

      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}
      {actionMessage && <div className="alert info">{actionMessage}</div>}

      <div className="monitor-toolbar">
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter workflows..."
          className="search-input"
        />
        <select
          value={statusFilter}
          onChange={e => setStatusFilter(e.target.value)}
          className="status-select"
        >
          {statusOptions.map(option => (
            <option key={option.value || 'all'} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>

      <div className="monitor-summary-strip">
        <span className={`engine-inline ${statusKind(engine.status)}`}>
          <span className="engine-dot" />
          <span>Engine:</span>
          <strong>{engine.status || 'UNKNOWN'}</strong>
        </span>
        <span className="summary-separator" />
        <SummaryItem tone="success" icon="✓" label="success" value={summary.success} />
        <SummaryItem tone="failed" icon="×" label="failed" value={summary.failed} />
        <SummaryItem tone="running" icon="▶" label="running" value={summary.running} />
        <SummaryItem tone="queued" icon="●" label="queued" value={summary.queued} />
        <span className="summary-separator" />
        <span className="summary-total"><strong>{summary.total}</strong> total</span>
        <span className="summary-updated">Updated: {formatDateTime(payload?.generatedAt)}</span>
      </div>

      <div className="table-card monitor-table-card">
        {loading ? <div className="empty-state">Loading monitor data...</div> : null}
        {!loading && filtered.length === 0 ? <div className="empty-state">No workflows match the current filters.</div> : null}
        {filtered.length > 0 && (
          <table className="workflow-table monitor-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Status</th>
                <th>Last Run</th>
                <th>Duration</th>
                <th>Schedule</th>
                <th>Next Run</th>
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
