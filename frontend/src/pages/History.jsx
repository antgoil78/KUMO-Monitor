import { useEffect, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'
import './History.css'

export default function History() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copiedRunId, setCopiedRunId] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())

  async function load() {
    try {
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

  return (
    <section className="page history-page">
      <div className="page-header">
        <div>
          <p className="eyebrow">KUMO Monitor</p>
          <h1>History</h1>
          <p className="page-subtitle">Run history. More filters will be added in the next iteration.</p>
        </div>
        <button className="button" onClick={load}>Refresh</button>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="table-card">
        {loading ? <div className="empty-state">Loading history...</div> : null}
        {!loading && rows.length === 0 ? <div className="empty-state">No history rows found.</div> : null}

        {rows.length > 0 && (
          <table className="workflow-table compact history-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th className="history-copy-heading">
                  <span className="history-sr-only">Copy run ID</span>
                </th>
                <th>Status</th>
                <th>Execution Time</th>
                <th>Start</th>
                <th>End</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, idx) => {
                const runId = String(r.RUN_ID || '')
                const workflowName = r.WORKFLOW_NAME || r.WORKFLOW_ID || '-'

                return (
                  <tr key={`${runId || idx}`}>
                    <td className="history-workflow-name">{workflowName}</td>
                    <td className="history-copy-cell">
                      {runId ? (
                        <button
                          type="button"
                          className={`small-button history-copy-button ${copiedRunId === runId ? 'copied' : ''}`}
                          title={`${copiedRunId === runId ? 'Copied' : 'Copy'} run ID: ${runId}`}
                          aria-label={`${copiedRunId === runId ? 'Copied' : 'Copy'} run ID`}
                          onClick={() => copyRunId(runId)}
                        >
                          <span className="history-copy-icon" aria-hidden="true" />
                        </button>
                      ) : (
                        <span className="muted-dash">-</span>
                      )}
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
