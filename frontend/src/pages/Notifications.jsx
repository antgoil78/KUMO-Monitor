import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Notifications() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)

  async function load() {
    try {
      setError(null)
      const data = await api.notifications()
      setRows(data.rows || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => { load() }, [])

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <p className="eyebrow">KUMO Monitor</p>
          <h1>Notifications</h1>
          <p className="page-subtitle">Administration of mail notifications will be built here.</p>
        </div>
        <button className="button" onClick={load}>Refresh</button>
      </div>
      {error && <div className="alert error">{error}</div>}
      <div className="table-card placeholder-card">
        <h2>Notification administration</h2>
        <p>This page is connected to `/api/notifications`, but edit/create forms are intentionally left for the next step.</p>
        <p>Rows loaded: <strong>{rows.length}</strong></p>
      </div>
    </section>
  )
}
