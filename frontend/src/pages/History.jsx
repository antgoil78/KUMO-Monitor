import { useEffect, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatDateTime } from '../utils/time.js'

export default function History() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

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

  useEffect(() => { load() }, [])

  return (
    <section className="page">
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
          <table className="workflow-table compact">
            <thead><tr><th>Run ID</th><th>Workflow ID</th><th>Status</th><th>Start</th><th>End</th></tr></thead>
            <tbody>
              {rows.map((r, idx) => (
                <tr key={`${r.RUN_ID || idx}`}>
                  <td><code>{r.RUN_ID || '-'}</code></td>
                  <td>{r.WORKFLOW_ID || '-'}</td>
                  <td><StatusBadge status={r.STATUS} /></td>
                  <td>{formatDateTime(r.START_TIME || r.REQUESTED_AT)}</td>
                  <td>{formatDateTime(r.END_TIME)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
