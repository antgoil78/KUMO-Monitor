import { useEffect, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatDateTime } from '../utils/time.js'
import './History.css'

export default function History() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copiedRunId, setCopiedRunId] = useState('')

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
                <th>Run ID</th>
                <th>Status</th>
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
                    <td>
                      <div className="history-run-id-cell">
                        <code title={runId || undefined}>{runId || '-'}</code>
                        {runId && (
                          <button
                            type="button"
                            className="small-button history-copy-button"
                            title="Copy run ID"
                            onClick={() => copyRunId(runId)}
                          >
                            {copiedRunId === runId ? 'Copied' : 'Copy'}
                          </button>
                        )}
                      </div>
                    </td>
                    <td><StatusBadge status={r.STATUS} /></td>
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
