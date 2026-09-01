import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'

function formatLogTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '--:--:--.---'
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3
  }).format(date)
}

export default function Admin() {
  const [error, setError] = useState(null)
  const [activities, setActivities] = useState([])
  const [activityFilter, setActivityFilter] = useState('ALL')
  const [activityPaused, setActivityPaused] = useState(false)

  useEffect(() => {
    if (activityPaused) return undefined
    let cancelled = false
    const loadActivities = () => api.adminActivityLog(750).then(result => {
      if (!cancelled) setActivities(result.activities || [])
    }).catch(err => {
      if (!cancelled) setError(err.message || String(err))
    })
    loadActivities()
    const id = window.setInterval(loadActivities, 2000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [activityPaused])

  const filteredActivities = useMemo(() => activityFilter === 'ALL' ? activities : activities.filter(item => item.category === activityFilter), [activities, activityFilter])

  return (
    <section className="page admin-page">
      <div className="page-header admin-header">
        <div><p className="breadcrumb">Application Administration / Application Log</p><h1>Application Log</h1><p>Live application, user, system, and Snowflake database activity.</p></div>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="vision-card admin-activity-card">
        <div className="card-title-row admin-log-title">
          <div><h3>Live application log</h3><span>User actions, application work, system activity, and every Snowflake call</span></div>
          <div className="admin-log-actions">
            <select value={activityFilter} onChange={event => setActivityFilter(event.target.value)}>
              <option value="ALL">All activity</option><option value="USER">User activity</option><option value="DATABASE">Database calls</option><option value="APPLICATION">Application</option><option value="SYSTEM">System</option>
            </select>
            <button type="button" className="button" onClick={() => setActivityPaused(value => !value)}>{activityPaused ? '▶ Resume' : 'Ⅱ Pause'}</button>
          </div>
        </div>
        <div className="admin-log-summary"><span>{filteredActivities.length} entries</span><span className="admin-log-live-dot" />{activityPaused ? 'Paused' : 'Live · updates every 2s'}</div>
        <div className="admin-terminal" role="log" aria-live="polite">
          <div className="admin-terminal-bar"><span className="terminal-dot red" /><span className="terminal-dot amber" /><span className="terminal-dot green" /><code>kumo-monitor --follow --newest-first</code></div>
          <div className="admin-terminal-output">
            {filteredActivities.length ? filteredActivities.map(item => {
              const details = item.details || {}
              const category = String(item.category || 'APPLICATION').toLowerCase()
              const status = String(item.status || 'SUCCESS').toLowerCase()
              const actorName = item.actor?.userName && item.actor.userName !== 'UNKNOWN'
                ? item.actor.userName
                : item.actor?.displayName && item.actor.displayName !== 'Unknown user'
                  ? item.actor.displayName
                  : item.category === 'USER' ? 'UNIDENTIFIED CLIENT' : 'BACKEND'
              const context = item.category === 'DATABASE'
                ? `${details.queryTag || 'SNOWFLAKE'} · ${actorName}`
                : actorName
              const metadata = [details.queryId && `query=${details.queryId}`, details.httpStatus && `http=${details.httpStatus}`, details.ipAddress && `ip=${details.ipAddress}`].filter(Boolean).join(' ')
              return (
                <div className={`terminal-entry ${status}`} key={item.id}>
                  <div className="terminal-line">
                    <span className="terminal-time">{formatLogTime(item.startedAt)}</span>
                    <span className={`terminal-category ${category}`}>[{item.category}]</span>
                    <span className={`terminal-status ${status}`}>{item.status}</span>
                    <span className="terminal-duration">{item.durationMs == null ? '   —   ' : `${String(item.durationMs).padStart(5, ' ')}ms`}</span>
                    <span className="terminal-context">{context}</span>
                    <span className="terminal-message">{item.label}</span>
                    {metadata && <span className="terminal-metadata">{metadata}</span>}
                  </div>
                  {item.error && <div className="terminal-error">↳ {item.error}</div>}
                </div>
              )
            }) : <div className="terminal-empty">$ waiting for application activity…</div>}
          </div>
        </div>
      </div>
    </section>
  )
}
