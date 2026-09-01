import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import StatusBadge, { statusKind } from '../components/StatusBadge.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import { formatDateTime, formatDateTimeSeconds } from '../utils/time.js'

function percent(value, total) {
  if (!total) return 0
  return Math.round((value / total) * 100)
}

function firstWord(value) {
  return String(value || '').trim().split(/\s+/)[0] || 'there'
}

let dashboardCache = null
const refreshOptions = [5, 10, 30, 60, 120]

function MetricCard({ label, value, delta, tone, icon, footer }) {
  return (
    <div className="vision-card metric-card">
      <div className="metric-copy">
        <span className="metric-label">{label}</span>
        <div className="metric-value-row">
          <strong>{value}</strong>
          {delta && <span className={`metric-delta ${tone || ''}`}>{delta}</span>}
        </div>
        {footer && <span className="metric-footer">{footer}</span>}
      </div>
      <div className={`metric-icon ${tone || ''}`}>{icon}</div>
    </div>
  )
}

function HealthCheck({ label, detail, ok, tone }) {
  const computedTone = tone || (ok ? 'success' : 'failed')
  return (
    <div className="health-row">
      <span className={`health-led ${computedTone}`} />
      <div>
        <strong>{label}</strong>
        <span>{detail}</span>
      </div>
    </div>
  )
}

function MonitorCacheHealth({ active, ageSeconds, lastRefreshAt, durationMs, phaseMs, refreshCount, refreshSeconds, ok }) {
  const cycleWaitSeconds = Math.max(0, Number(refreshSeconds || 0) - (Number(durationMs || 0) / 1000))
  const nextRefreshSeconds = Math.max(0, Math.ceil(cycleWaitSeconds - ageSeconds))
  return (
    <div className={`health-row monitor-cache-health ${active ? 'refreshing' : ''}`}>
      <div className="cache-refresh-graphic" aria-hidden="true">
        <span className="cache-refresh-ring" />
        <span className="cache-refresh-core">↻</span>
      </div>
      <div>
        <strong>Monitor cache</strong>
        <span className="cache-refresh-state">{active ? 'Refreshing from Snowflake…' : 'Waiting for next refresh'}</span>
        <span>{ok ? `Data freshness: ${ageSeconds} seconds old` : 'No cache data available'}</span>
        <span>{ok ? `Backend received Snowflake data: ${formatDateTimeSeconds(lastRefreshAt)}` : 'Backend has not received Snowflake data yet'}</span>
        <span>{active ? 'Next refresh: in progress' : `Next refresh starts in approximately ${nextRefreshSeconds} seconds`}</span>
        <span>Completed cycle #{refreshCount || 0}{durationMs != null ? ` · Snowflake query took ${durationMs}ms` : ''}</span>
        {phaseMs && <span>Connect {phaseMs.connection ?? '—'}ms · workflows {phaseMs.workflows ?? '—'}ms · engine {phaseMs.engine ?? '—'}ms</span>}
      </div>
    </div>
  )
}

function ActiveUsersCard({ users = [], currentUserName }) {
  const normalized = Array.isArray(users) ? users : []

  return (
    <div className="vision-card identity-card active-users-card">
      <div className="card-title-row identity-title-row">
        <div>
          <h3>Current logged-in users</h3>
          <span>Application session registry</span>
        </div>
        <strong className="active-user-count">{normalized.length}</strong>
      </div>

      <div className="active-user-list">
        {normalized.length === 0 ? (
          <div className="soft-empty">No active users have been registered yet.</div>
        ) : normalized.map(user => {
          const displayName = user.displayName || user.userName || 'Unknown user'
          const isCurrent = String(user.userName || '').toUpperCase() === String(currentUserName || '').toUpperCase()
          return (
            <div className={`active-user-row ${isCurrent ? 'current' : ''}`} key={user.userName || displayName}>
              <div className="active-user-avatar">{displayName.slice(0, 1).toUpperCase()}</div>
              <div className="active-user-main">
                <strong>{displayName}</strong>
                <span>{user.roleName || 'Unknown role'}</span>
                <small>Last seen {formatDateTime(user.lastSeenAt)}</small>
              </div>
              {isCurrent && <em>You</em>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function WorkflowActivity({ workflow }) {
  if (!workflow) return null
  const kind = statusKind(workflow.lastStatus)
  return (
    <div className="activity-row">
      <span className={`activity-dot ${kind}`} />
      <div>
        <strong>{workflow.workflowName}</strong>
        <span>{formatDateTime(workflow.lastStartTime)} · {workflow.workflowGroup || 'Ungrouped'}</span>
      </div>
      <StatusBadge status={workflow.lastStatus} />
    </div>
  )
}

function TinyBarChart({ workflows }) {
  const groups = useMemo(() => {
    const map = new Map()
    workflows.forEach(w => {
      const group = w.workflowGroup || 'Other'
      if (!map.has(group)) map.set(group, { group, total: 0, success: 0, failed: 0, running: 0 })
      const item = map.get(group)
      item.total += 1
      const kind = statusKind(w.lastStatus)
      if (kind === 'success') item.success += 1
      if (kind === 'failed') item.failed += 1
      if (kind === 'running') item.running += 1
    })
    return Array.from(map.values()).slice(0, 8)
  }, [workflows])

  const maxTotal = Math.max(1, ...groups.map(g => g.total))
  return (
    <div className="tiny-chart">
      {groups.map(item => (
        <div className="tiny-bar-col" key={item.group}>
          <div className="tiny-bar-track">
            <div className="tiny-bar-fill" style={{ height: `${Math.max(10, (item.total / maxTotal) * 100)}%` }} />
          </div>
          <span title={item.group}>{item.group.slice(0, 5)}</span>
        </div>
      ))}
    </div>
  )
}

function browserName(userAgent) {
  const value = String(userAgent || '')
  if (/Edg\//.test(value)) return 'Edge'
  if (/Chrome\//.test(value)) return 'Chrome'
  if (/Firefox\//.test(value)) return 'Firefox'
  if (/Safari\//.test(value)) return 'Safari'
  return value ? 'Browser' : 'Unknown'
}

function PollingInfo({ clients, pollingActive, onClose }) {
  const clientCount = clients.length
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="polling-info-title" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      <div className="vision-modal polling-info-modal">
        <div className="modal-header">
          <div>
            <span className="modal-eyebrow">Live updates</span>
            <h2 id="polling-info-title">Client polling information</h2>
            <p>{clientCount} browser client{clientCount === 1 ? '' : 's'} currently connected</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="polling-client-state"><span className={`topbar-dot ${pollingActive ? 'success' : 'failed'}`} />{pollingActive ? 'Live polling active' : 'Live polling stopped'}</div>
        {clients.length ? <div className="polling-client-list">
          {clients.map((client, index) => (
            <article className="polling-client-row" key={`${client.clientId || client.ipAddress}-${index}`}>
              <div className="polling-client-avatar">{String(client.displayName || client.userName || '?').slice(0, 1).toUpperCase()}</div>
              <div className="polling-client-identity"><strong>{client.displayName || client.userName || 'Unknown user'}</strong><span>{client.userName || 'UNKNOWN'} · {client.roleName || 'Unknown role'}</span></div>
              <dl>
                <div><dt>Page</dt><dd>{client.page || 'Unknown page'}</dd></div>
                <div><dt>IP address</dt><dd>{client.ipAddress || 'Unavailable'}</dd></div>
                <div><dt>Browser</dt><dd title={client.userAgent || ''}>{browserName(client.userAgent)}</dd></div>
                <div><dt>Connected</dt><dd>{formatDateTime(client.connectedAt)}</dd></div>
                <div><dt>Last activity</dt><dd>{formatDateTime(client.lastActivityAt)}</dd></div>
              </dl>
            </article>
          ))}
        </div> : <div className="soft-empty">No live browser clients are connected.</div>}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const cacheAnimationTimer = useRef(null)
  const [payload, setPayload] = useState(dashboardCache?.payload || null)
  const [health, setHealth] = useState(dashboardCache?.health || null)
  const [ping, setPing] = useState(dashboardCache?.ping || null)
  const [session, setSession] = useState(dashboardCache?.session || null)
  const [activeUsers, setActiveUsers] = useState(dashboardCache?.activeUsers || [])
  const [realtime, setRealtime] = useState(dashboardCache?.realtime || null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(!dashboardCache)
  const [refreshing, setRefreshing] = useState(Boolean(dashboardCache))
  const [showPollingInfo, setShowPollingInfo] = useState(false)
  const [nowMs, setNowMs] = useState(Date.now())
  const [cacheRefreshing, setCacheRefreshing] = useState(Boolean(dashboardCache?.realtime?.monitorCache?.refreshing))
  const [lastClientRefreshAt, setLastClientRefreshAt] = useState(dashboardCache?.cachedAt || null)
  const [refreshIntervalSec, setRefreshIntervalSec] = useState(
    Number(window.sessionStorage.getItem('kumoDashboardClientRefreshSeconds') || 10)
  )

  async function load({ silent = false } = {}) {
    if (silent) {
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setError(null)

    try {
      const data = await api.dashboard()
      const monitorData = data.monitor || dashboardCache?.payload || null
      const healthData = data.health || dashboardCache?.health || null
      const pingData = data.ping || dashboardCache?.ping || null
      const sessionData = data.session || dashboardCache?.session || null
      const usersData = data.activeUsers?.users || dashboardCache?.activeUsers || []
      const realtimeData = data.realtime || dashboardCache?.realtime || null
      const settingsData = data.settings || dashboardCache?.settings || null

      const receivedAt = new Date().toISOString()
      dashboardCache = {
        payload: monitorData,
        health: healthData,
        ping: pingData,
        session: sessionData,
        activeUsers: usersData,
        realtime: realtimeData,
        settings: settingsData,
        cache: data.cache || null,
        cachedAt: receivedAt
      }

      setPayload(monitorData)
      setHealth(healthData)
      setPing(pingData)
      setSession(sessionData)
      setActiveUsers(usersData)
      setRealtime(realtimeData)
      setLastClientRefreshAt(receivedAt)
      if (data.cache?.dashboardError) setError(`Dashboard cache: ${data.cache.dashboardError}`)
    } catch (err) {
      if (!dashboardCache) setError(err.message || String(err))
      if (dashboardCache) {
        setPayload(dashboardCache.payload)
        setHealth(dashboardCache.health)
        setPing(dashboardCache.ping)
        setSession(dashboardCache.session)
        setActiveUsers(dashboardCache.activeUsers || [])
        setRealtime(dashboardCache.realtime || null)
      }
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    let cancelled = false
    api.session().then(sessionData => {
      if (cancelled) return
      dashboardCache = { ...(dashboardCache || {}), session: sessionData }
      setSession(sessionData)
    }).catch(err => {
      if (!cancelled && !session) setError(err.message || String(err))
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    function handleRealtime(browserEvent) {
      const event = browserEvent.detail
      if (event?.type === 'connected') {
        load({ silent: Boolean(dashboardCache) })
      }
      if (event?.type === 'realtime_state') {
        const realtimeData = event.data || null
        const liveUsersByName = new Map()
        for (const client of realtimeData?.clients || []) {
          const key = String(client.userName || '').trim().toUpperCase()
          if (!key || key === 'UNKNOWN') continue
          const existing = liveUsersByName.get(key)
          const lastSeenAt = client.lastActivityAt || client.connectedAt
          if (!existing) {
            liveUsersByName.set(key, {
              userName: client.userName,
              displayName: client.displayName || client.userName,
              roleName: client.roleName || 'Unknown role',
              firstSeenAt: client.connectedAt,
              lastSeenAt,
              connectionCount: 1
            })
          } else {
            existing.connectionCount += 1
            if (lastSeenAt && lastSeenAt > (existing.lastSeenAt || '')) existing.lastSeenAt = lastSeenAt
            if (client.connectedAt && client.connectedAt < (existing.firstSeenAt || client.connectedAt)) existing.firstSeenAt = client.connectedAt
          }
        }
        const liveUsers = [...liveUsersByName.values()].sort((a, b) => String(b.lastSeenAt || '').localeCompare(String(a.lastSeenAt || '')))
        dashboardCache = {
          ...(dashboardCache || {}),
          realtime: realtimeData,
          activeUsers: liveUsers,
          cachedAt: new Date().toISOString()
        }
        setRealtime(realtimeData)
        setActiveUsers(liveUsers)
      }
      if (event?.type === 'monitor_refresh_started') {
        if (cacheAnimationTimer.current) clearTimeout(cacheAnimationTimer.current)
        setCacheRefreshing(true)
      }
      if (event?.type === 'monitor_refresh_completed') {
        const monitorDiagnostics = event.data || {}
        setRealtime(previous => ({
          ...(previous || {}),
          monitorCache: monitorDiagnostics
        }))
        if (cacheAnimationTimer.current) clearTimeout(cacheAnimationTimer.current)
        cacheAnimationTimer.current = setTimeout(() => setCacheRefreshing(false), 700)
      }
    }
    window.addEventListener('kumo:realtime', handleRealtime)
    return () => {
      window.removeEventListener('kumo:realtime', handleRealtime)
      if (cacheAnimationTimer.current) clearTimeout(cacheAnimationTimer.current)
    }
  }, [])

  useEffect(() => {
    load({ silent: Boolean(dashboardCache) })
    const id = setInterval(() => load({ silent: true }), Math.max(5, refreshIntervalSec || 10) * 1000)
    return () => clearInterval(id)
  }, [refreshIntervalSec])

  function updateRefreshInterval(value) {
    const seconds = Number(value)
    setRefreshIntervalSec(seconds)
    window.sessionStorage.setItem('kumoDashboardClientRefreshSeconds', String(seconds))
  }

  const workflows = payload?.workflows || []
  const summary = payload?.summary || { total: 0, success: 0, failed: 0, running: 0, queued: 0 }
  const engine = payload?.engine || { status: 'UNKNOWN' }
  const successRate = percent(summary.success, summary.total)
  const activeCount = Number(summary.running || 0) + Number(summary.queued || 0)
  const failedWorkflows = workflows.filter(w => statusKind(w.lastStatus) === 'failed')
  const runningWorkflows = workflows.filter(w => ['running', 'queued'].includes(statusKind(w.lastStatus)))
  const recent = workflows
    .slice()
    .sort((a, b) => String(b.lastStartTime || '').localeCompare(String(a.lastStartTime || '')))
    .slice(0, 6)

  const mockMode = Boolean(health?.mock)
  const snowflakeOk = Boolean(ping?.ok)
  const warehouse = ping?.snowflake?.WAREHOUSE_NAME || ping?.snowflake?.warehouse_name || 'Not selected'
  const role = session?.roleName || ping?.snowflake?.ROLE_NAME || ping?.snowflake?.role_name || 'Unknown role'
  const displayName = session?.displayName || session?.userName || 'KUMO user'
  const welcomeName = session?.firstName || firstWord(displayName)
  const callerRightsActive = Boolean(session?.callerRightsActive)
  const connectedClients = Number(realtime?.clientCount || 0)
  const pollingActive = Boolean(realtime?.pollingActive)
  const monitorCache = realtime?.monitorCache || {}
  const dashboardRuntimeCache = realtime?.dashboardCache || {}
  const activityLease = realtime?.activityLease || {}
  const engineKind = statusKind(engine.status)
  const engineOk = ['success', 'running'].includes(engineKind)
  const cacheFresh = Boolean(payload?.generatedAt)
  const monitorLastRefreshAt = monitorCache.lastRefreshAt || payload?.generatedAt
  const parsedMonitorRefreshAt = Date.parse(monitorLastRefreshAt || '')
  const monitorCacheAgeSeconds = Number.isFinite(parsedMonitorRefreshAt)
    ? Math.max(0, Math.floor((nowMs - parsedMonitorRefreshAt) / 1000))
    : Math.max(0, Math.floor(Number(monitorCache.lastRefreshAgeSeconds || 0)))

  return (
    <section className="page dashboard-page">
      <div className="dashboard-topbar">
        <div>
          <p className="breadcrumb">Pages / Dashboard</p>
          <h1>Dashboard</h1>
        </div>
        <div className="dashboard-top-actions">
          <div className="topbar-user" title={`${displayName} · ${role}`}>
            <span>{displayName.slice(0, 1).toUpperCase()}</span>
            <div><strong>{displayName}</strong><small>{role}</small></div>
          </div>
          <div className="topbar-status">
            <span className={`topbar-dot ${snowflakeOk ? 'success' : mockMode ? 'queued' : 'failed'}`} />
            <span title={ping?.error || ''}>{mockMode ? 'Mock mode' : snowflakeOk ? 'Snowflake connected' : 'Snowflake check failed'}</span>
          </div>
          <button type="button" className="topbar-status realtime-status" onClick={() => setShowPollingInfo(true)} aria-label="Show client polling information">
            <span className={`topbar-dot ${pollingActive ? 'success' : 'failed'}`} />
            <span>{connectedClients} client{connectedClients === 1 ? '' : 's'} · {pollingActive ? 'polling on' : 'polling off'}</span>
            <span className="realtime-info-icon" aria-hidden="true">i</span>
          </button>
          <label className="topbar-refresh-control">
            <span>Client refresh</span>
            <select value={refreshIntervalSec} onChange={event => updateRefreshInterval(event.target.value)}>
              {refreshOptions.map(seconds => <option key={seconds} value={seconds}>{seconds}s</option>)}
            </select>
          </label>
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}

      <div className="metric-grid vision-grid-4">
        <MetricCard
          label="Total workflows"
          value={loading ? '—' : summary.total}
          delta={`${successRate}% OK`}
          tone="success"
          icon="▦"
          footer="Configured monitor objects"
        />
        <MetricCard
          label="Currently active"
          value={loading ? '—' : activeCount}
          delta={`${summary.running || 0} running`}
          tone="running"
          icon="▶"
          footer={`${summary.queued || 0} queued / pending`}
        />
        <MetricCard
          label="Failed latest runs"
          value={loading ? '—' : summary.failed}
          delta={summary.failed ? 'Needs attention' : 'Clean' }
          tone={summary.failed ? 'failed' : 'success'}
          icon="!"
          footer="Based on latest workflow status"
        />
        <MetricCard
          label="Shared cache refresh"
          value={`${monitorCache.refreshSeconds || health?.refreshSeconds || payload?.refreshIntervalMs / 1000 || 5}s`}
          delta="Snowflake → backend"
          tone="queued"
          icon="↻"
          footer={`All clients share data from ${formatDateTime(payload?.generatedAt)}`}
        />
      </div>

      <div className="dashboard-layout session-layout">
        <div className="vision-card welcome-card">
          <div className="welcome-content">
            <span className="eyebrow">KUMO Monitor</span>
            <h2>Welcome back, {welcomeName}</h2>
            <p>
              Your workflow estate is being monitored in Snowpark Container Services.
              Your current Snowflake identity and role are shown below.
            </p>
            <div className="welcome-user-panel">
              <div className="welcome-avatar">{displayName.slice(0, 1).toUpperCase()}</div>
              <div>
                <strong>{displayName}</strong>
                <span>{session?.userName || 'UNKNOWN'} · {role}</span>
              </div>
            </div>
            <div className="welcome-actions">
              <span className={`glass-pill ${engineOk ? 'success' : 'failed'}`}>Engine {engine.status || 'UNKNOWN'}</span>
              <span className="glass-pill">{summary.total || 0} workflows</span>
              <span className="glass-pill">{role}</span>
              <span className={`glass-pill ${callerRightsActive ? 'success' : 'failed'}`}>{callerRightsActive ? 'Real user context' : 'Service context'}</span>
            </div>
          </div>
          <div className="orb-stage" aria-hidden="true">
            <div className="orb orb-main" />
            <div className="orb-ring ring-one" />
            <div className="orb-ring ring-two" />
          </div>
        </div>

        <ActiveUsersCard users={activeUsers} currentUserName={session?.userName} />

        <div className="vision-card satisfaction-card">
          <div className="card-title-row">
            <div>
              <h3>Workflow Success Rate</h3>
              <span>Latest run status</span>
            </div>
          </div>
          <div className="radial-meter" style={{ '--meter': `${successRate}%` }}>
            <div className="radial-core">
              <strong>{successRate}%</strong>
              <span>{summary.success || 0}/{summary.total || 0} OK</span>
            </div>
          </div>
          <div className="radial-scale"><span>0%</span><span>100%</span></div>
        </div>

        <div className="vision-card health-card">
          <div className="card-title-row">
            <div>
              <h3>Health Checks</h3>
              <span>Runtime and Snowflake session</span>
            </div>
          </div>
          <div className="health-list">
            <HealthCheck label="Backend API" detail={health?.app || 'KUMO Monitor'} ok={Boolean(health?.ok)} />
            <HealthCheck label="Snowflake session" detail={ping?.mode || health?.snowflakeConnectionMode || 'unknown'} ok={snowflakeOk || mockMode} tone={mockMode ? 'queued' : undefined} />
            <HealthCheck label="Warehouse" detail={warehouse} ok={snowflakeOk && warehouse !== 'Not selected'} />
            <HealthCheck label="Workflow engine" detail={engine.status || 'UNKNOWN'} ok={engineOk} tone={engineKind} />
            <MonitorCacheHealth
              active={cacheRefreshing || Boolean(monitorCache.refreshing)}
              ageSeconds={monitorCacheAgeSeconds}
              lastRefreshAt={monitorLastRefreshAt}
              durationMs={monitorCache.lastDurationMs}
              phaseMs={monitorCache.lastPhaseMs}
              refreshCount={monitorCache.refreshCount}
              refreshSeconds={monitorCache.refreshSeconds || health?.refreshSeconds || 5}
              ok={cacheFresh}
            />
            <HealthCheck
              label="Client cache read"
              detail={`Every ${refreshIntervalSec}s · last read ${formatDateTimeSeconds(lastClientRefreshAt)}`}
              ok={Boolean(lastClientRefreshAt)}
              tone="running"
            />
            <HealthCheck
              label="Connected clients"
              detail={`${connectedClients} SSE connection${connectedClients === 1 ? '' : 's'}`}
              ok={connectedClients > 0}
              tone={connectedClients > 0 ? 'success' : 'failed'}
            />
            <HealthCheck
              label="Backend polling"
              detail={`${pollingActive ? 'Active' : 'Stopped'} · activity lease ${Math.ceil(Number(activityLease.remainingSeconds || 0))}s · monitor #${monitorCache.refreshCount || 0} · dashboard #${dashboardRuntimeCache.refreshCount || 0}`}
              ok={pollingActive}
              tone={pollingActive ? 'success' : 'failed'}
            />
          </div>
        </div>
      </div>

      <div className="dashboard-bottom-grid">
        <div className="vision-card chart-card">
          <div className="card-title-row">
            <div>
              <h3>Workflow Distribution</h3>
              <span>Groups from current monitor payload</span>
            </div>
          </div>
          <TinyBarChart workflows={workflows} />
          <div className="chart-stats">
            <div><strong>{summary.success || 0}</strong><span>Success</span></div>
            <div><strong>{summary.failed || 0}</strong><span>Failed</span></div>
            <div><strong>{summary.running || 0}</strong><span>Running</span></div>
            <div><strong>{summary.queued || 0}</strong><span>Queued</span></div>
          </div>
        </div>

        <div className="vision-card active-card">
          <div className="card-title-row">
            <div>
              <h3>Active Workflows</h3>
              <span>Running and queued jobs</span>
            </div>
          </div>
          {runningWorkflows.length === 0 ? (
            <div className="soft-empty">No active workflows right now.</div>
          ) : runningWorkflows.slice(0, 5).map(workflow => (
            <div className="active-run" key={workflow.workflowId}>
              <div>
                <strong>{workflow.workflowName}</strong>
                <span>{workflow.lastStatus || 'UNKNOWN'}</span>
              </div>
              <ProgressBar progress={workflow.progress} status={workflow.lastStatus} />
            </div>
          ))}
        </div>

        <div className="vision-card activity-card">
          <div className="card-title-row">
            <div>
              <h3>Recent Activity</h3>
              <span>Latest workflow runs</span>
            </div>
          </div>
          <div className="activity-list">
            {recent.length ? recent.map(w => <WorkflowActivity key={`${w.workflowId}-${w.lastRunId || ''}`} workflow={w} />) : <div className="soft-empty">No recent workflow activity.</div>}
          </div>
        </div>

        <div className="vision-card risk-card">
          <div className="card-title-row">
            <div>
              <h3>Attention</h3>
              <span>Failed latest runs</span>
            </div>
          </div>
          {failedWorkflows.length === 0 ? (
            <div className="soft-empty success-text">No failed workflows in latest status.</div>
          ) : failedWorkflows.slice(0, 5).map(workflow => (
            <div className="risk-row" key={workflow.workflowId}>
              <span>×</span>
              <div>
                <strong>{workflow.workflowName}</strong>
                <small>{workflow.workflowGroup || 'Ungrouped'} · {formatDateTime(workflow.lastStartTime)}</small>
              </div>
            </div>
          ))}
        </div>
      </div>
      {showPollingInfo && <PollingInfo clients={realtime?.clients || []} pollingActive={pollingActive} onClose={() => setShowPollingInfo(false)} />}
    </section>
  )
}
