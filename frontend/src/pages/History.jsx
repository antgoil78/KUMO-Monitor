import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'
import './History.css'

export default function History({ workflowName = '', workflowId = '', onNavigate }) {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copiedRunId, setCopiedRunId] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [workflowFilter, setWorkflowFilter] = useState(workflowName)
  const [selectedWorkflowId, setSelectedWorkflowId] = useState(workflowId)
  const [statusFilter, setStatusFilter] = useState('')

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
      const rowWorkflowName = String(row.WORKFLOW_NAME || '').toLowerCase()
      const rowWorkflowId = String(row.WORKFLOW_ID || '')
      const status = String(row.STATUS || '').trim().toUpperCase()
      const matchesSelectedWorkflow = !selectedWorkflowId || rowWorkflowId === String(selectedWorkflowId)
      const matchesName = !nameFilter || rowWorkflowName.includes(nameFilter)
      const matchesStatus = !selectedStatus || status === selectedStatus
      return matchesSelectedWorkflow && matchesName && matchesStatus
    })
  }, [rows, workflowFilter, statusFilter, selectedWorkflowId])

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
          onChange={event => {
            setWorkflowFilter(event.target.value)
            setSelectedWorkflowId('')
          }}
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
                          disabled={!runId}
                          onClick={() => onNavigate('executionLog', {
                            runId,
                            workflowId: r.WORKFLOW_ID,
                            workflowName,
                            returnPage: 'history'
                          })}
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
    </section>
  )
}
