import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'
import './History.css'

function HistoryLogModal({ row, onClose }) {
  if (!row) return null

  const workflowName = row.WORKFLOW_NAME || row.WORKFLOW_ID || 'Workflow'
  const runId = String(row.RUN_ID || '')

  return (
    <div className="modal-backdrop history-log-backdrop" role="dialog" aria-modal="true" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      <div className="vision-modal history-log-modal">
        <div className="modal-header">
          <div>
            <span className="modal-eyebrow">Execution log</span>
            <h2>{workflowName}</h2>
            <p>{runId ? `Run ID: ${runId}` : 'Run ID unavailable'}</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="history-log-placeholder">
          <strong>Log details will be added here later.</strong>
          <span>Status: {row.STATUS || '-'}</span>
          <span>Started: {formatDateTime(row.START_TIME || row.REQUESTED_AT)}</span>
        </div>
      </div>
    </div>
  )
}

export default function History() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copiedRunId, setCopiedRunId] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [workflowFilter, setWorkflowFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [logRow, setLogRow] = useState(null)

  async function load() {
    try {
      setLoading(true)
      setError(null)
      const data = await api.history(300)
      setRows(data.rows || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function copyRunId(runId) {
    const value = String(runId || '').trim()
    if (!value) return

    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value)
      } else {
        throw new Error('Clipboard API unavailable')
      }
    } catch (_) {
      const textArea = document.createElement('textarea')
      textArea.value = value
      textArea.setAttribute('readonly', '')
      textArea.style.position = 'fixed'
      textArea.style.opacity = '0'
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
    }

    setCopiedRunId(value)
  }

  useEffect(() => { load() }, [])

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 15000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!copiedRunId) return undefined
    const timer = window.setTimeout(() => setCopiedRunId(''), 1400)
    return () => window.clearTimeout(timer)
  }, [copiedRunId])

  const statusOptions = useMemo(() => {
    const statuses = new Set()
    rows.forEach(row => {
      const status = String(row.STATUS || '').trim().toUpperCase()
      if (status) statuses.add(status)
    })
    return Array.from(statuses).sort()
  }, [rows])

  const filteredRows = useMemo(() => {
    const nameFilter = workflowFilter.trim().toLowerCase()
    const selectedStatus = statusFilter.trim().toUpperCase()

    return rows.filter(row => {
      const workflowName = String(row.WORKFLOW_NAME || '').toLowerCase()
      const status = String(row.STATUS || '').trim().toUpperCase()
      const matchesName = !nameFilter || workflowName.includes(nameFilter)
      const matchesStatus = !selectedStatus || status === selectedStatus
      return matchesName && matchesStatus
    })
  }, [rows, workflowFilter, statusFilter])

  return (
    <section className="page history-page">
      <div className="page-header">
        <div>
          <p className="eyebrow">KUMO Monitor</p>
          <h1>History</h1>
          <p className="page-subtitle">Run history by workflow, status and execution time.</p>
        </div>
        <button className="button" onClick={load}>Refresh</button>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="monitor-toolbar history-toolbar">
        <input
          value={workflowFilter}
          onChange={event => setWorkflowFilter(event.target.value)}
          placeholder="Filter workflow name..."
          className="search-input"
          aria-label="Filter history by workflow name"
        />
        <select
          value={statusFilter}
          onChange={event => setStatusFilter(event.target.value)}
          className="status-select"
          aria-label="Filter history by status"
        >
          <option value="">All statuses</option>
          {statusOptions.map(status => (
            <option key={status} value={status}>{status}</option>
          ))}
        </select>
      </div>

      <div className="table-card">
        {loading ? <div className="empty-state">Loading history...</div> : null}
        {!loading && rows.length === 0 ? <div className="empty-state">No history rows found.</div> : null}
        {!loading && rows.length > 0 && filteredRows.length === 0 ? <div className="empty-state">No history rows match the current filters.</div> : null}

        {filteredRows.length > 0 && (
          <table className="workflow-table compact history-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th className="history-copy-heading">
                  <span className="history-sr-only">Run actions</span>
                </th>
                <th>Status</th>
                <th>Execution Time</th>
                <th>Start</th>
                <th>End</th>
              </tr>
            </thead>
            <tbody>
              {filteredRows.map((r, idx) => {
                const runId = String(r.RUN_ID || '')
                const workflowName = r.WORKFLOW_NAME || r.WORKFLOW_ID || '-'

                return (
                  <tr key={`${runId || idx}`}>
                    <td className="history-workflow-name">{workflowName}</td>
                    <td className="history-copy-cell">
                      <div className="history-action-buttons">
                        {runId ? (
                          <button
                            type="button"
                            className={`small-button history-icon-button history-copy-button ${copiedRunId === runId ? 'copied' : ''}`}
                            title={`${copiedRunId === runId ? 'Copied' : 'Copy'} run ID: ${runId}`}
                            aria-label={`${copiedRunId === runId ? 'Copied' : 'Copy'} run ID`}
                            onClick={() => copyRunId(runId)}
                          >
                            <span className="history-copy-icon" aria-hidden="true" />
                          </button>
                        ) : (
                          <span className="muted-dash">-</span>
                        )}
                        <button
                          type="button"
                          className="small-button history-icon-button history-log-button"
                          title={`Open log for ${workflowName}`}
                          aria-label={`Open log for ${workflowName}`}
                          onClick={() => setLogRow(r)}
                        >
                          <span className="history-log-icon" aria-hidden="true" />
                        </button>
                      </div>
                    </td>
                    <td><StatusBadge status={r.STATUS} /></td>
                    <td className="duration-cell">{elapsedDuration(r.START_TIME, r.END_TIME, r.STATUS, nowMs)}</td>
                    <td>{formatDateTime(r.START_TIME || r.REQUESTED_AT)}</td>
                    <td>{formatDateTime(r.END_TIME)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <HistoryLogModal row={logRow} onClose={() => setLogRow(null)} />
    </section>
  )
}
