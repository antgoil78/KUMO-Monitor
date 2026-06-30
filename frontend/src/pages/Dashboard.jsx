import { useEffect, useMemo, useState } from 'react'
import { api, createKumoEventSource } from '../api.js'
import StatusBadge, { statusKind } from '../components/StatusBadge.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import { formatDateTime } from '../utils/time.js'

function resolveSettled(result, fallback = null) {
  return result.status === 'fulfilled' ? result.value : fallback
}

function percent(value, total) {
  if (!total) return 0
  return Math.round((value / total) * 100)
}

function firstWord(value) {
  return String(value || '').trim().split(/\s+/)[0] || 'there'
}

function normalizeStatus(status, fallback = 'INITIATING') {
  const value = String(status || '').trim().toUpperCase()
  return value || fallback
}

function buildSummary(workflows) {
  const rows = workflows || []
  return {
    total: rows.length,
    success: rows.filter(w => statusKind(w.lastStatus) === 'success').length,
    failed: rows.filter(w => statusKind(w.lastStatus) === 'failed').length,
    running: rows.filter(w => statusKind(w.lastStatus) === 'running').length,
    queued: rows.filter(w => statusKind(w.lastStatus) === 'queued').length
  }
}

function applyRealtimeRun(payload, data) {
  if (!payload?.workflows?.length || !(data?.workflowId || data?.lock?.workflowId)) return payload

  const workflowId = String(data.workflowId || data.lock?.workflowId)
  const runId = data.runId || data.lock?.runId || ''
  const status = normalizeStatus(data.status || data.lock?.status, 'QUEUED')

  const workflows = payload.workflows.map(workflow => {
    if (String(workflow.workflowId) !== workflowId) return workflow

    const active = ['INITIATING', 'REQUESTED', 'PENDING', 'SCHEDULED', 'QUEUED', 'STARTING', 'RUNNING', 'IN_PROGRESS', 'EXECUTING'].includes(status)

    return {
      ...workflow,
      lastStatus: status,
      lastRunId: runId || workflow.lastRunId,
      lastRequestedAt: data.requestedAt || data.lock?.requestedAt || workflow.lastRequestedAt,
      lastRequestedBy: data.requestedBy || data.lock?.requestedBy || data.actor?.displayName || workflow.lastRequestedBy,
      runLocked: active,
      runLock: active ? { ...(workflow.runLock || {}), ...(data.lock || {}), status, runId } : workflow.runLock
    }
  })

  return { ...payload, workflows, summary: buildSummary(workflows), generatedAt: payload.generatedAt || new Date().toISOString() }
}

let dashboardCache = null

function MetricCard({ label, value, delta, tone, icon, footer }) {
  return (
    <div className={`metric-card ${tone || ''}`}>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {delta && <small>{delta}</small>}
        {footer && <p>{footer}</p>}
      </div>
      <span className="metric-icon">{icon}</span>
    </div>
  )
}

function HealthCheck({ label, detail, ok, tone }) {
  const computedTone = tone || (ok ? 'success' : 'failed')
  return (
    <div className={`health-check ${computedTone}`}>
      <span className="health-dot" />
      <div>
        <strong>{label}</strong>
        <span>{detail}</span>
      </div>
    </div>
  )
}

function ActiveUsersCard({ users = [], currentUserName }) {
  const normalized = Array.isArray(users) ? users : []

  return (
    <div className="vision-card active-users-card">
      <div className="card-heading">
        <div>
          <h3>Current logged-in users</h3>
          <p>Application session registry</p>
        </div>
        <strong>{normalized.length}</strong>
      </div>

      <div className="active-user-list">
        {normalized.length === 0 ? (
          <div className="soft-empty">No active users have been registered yet.</div>
        ) : normalized.map(user => {
          const displayName = user.displayName || user.userName || 'Unknown user'
          const isCurrent = String(user.userName || '').toUpperCase() === String(currentUserName || '').toUpperCase()
          return (
            <div className="active-user-row" key={`${user.userName || displayName}-${user.lastSeenAt || ''}`}>
              <span className="avatar-dot">{displayName.slice(0, 1).toUpperCase()}</span>
              <div>
                <strong>{displayName}</strong>
                <span>{user.roleName || 'Unknown role'} · Last seen {formatDateTime(user.lastSeenAt)}</span>
              </div>
              {isCurrent && <em>You</em>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function CurrentUserCard({ session, role, warehouse }) {
  const displayName = session?.displayName || session?.userName || 'KUMO user'
  const userName = session?.userName || 'UNKNOWN'
  const activeRole = session?.roleName || role || 'Unknown role'
  const mode = session?.mode || 'unknown'
  const callerActive = Boolean(session?.callerRightsActive)
  const tokenPresent = Boolean(session?.callerTokenPresent)

  return (
    <div className="vision-card current-user-card">
      <div className="current-user-head">
        <span className="user-avatar large">{displayName.slice(0, 1).toUpperCase()}</span>
        <div>
          <span>Current logged-in user</span>
          <h3>{displayName}</h3>
        </div>
      </div>
      <dl className="session-grid">
        <div><dt>Username</dt><dd>{userName}</dd></div>
        <div><dt>Role</dt><dd>{activeRole}</dd></div>
        <div><dt>Warehouse</dt><dd>{warehouse}</dd></div>
        <div><dt>Session mode</dt><dd>{mode}</dd></div>
      </dl>
      <p className="session-note">{callerActive ? 'Caller rights active' : tokenPresent ? 'Caller token received' : 'Service user mode'}</p>
    </div>
  )
}

function WorkflowActivity({ workflow }) {
  if (!workflow) return null
  return (
    <div className="activity-row">
      <StatusBadge status={workflow.lastStatus} />
      <div>
        <strong>{workflow.workflowName}</strong>
        <span>{formatDateTime(workflow.lastStartTime)} · {workflow.workflowGroup || 'Ungrouped'}</span>
      </div>
    </div>
  )
}

function TinyBarChart({ workflows }) {
  const groups = useMemo(() => {
    const map = new Map()
    workflows.forEach(workflow => {
      const group = workflow.workflowGroup || 'Other'
      if (!map.has(group)) map.set(group, { group, total: 0, success: 0, failed: 0, running: 0 })
      const item = map.get(group)
      item.total += 1
      const kind = statusKind(workflow.lastStatus)
      if (kind === 'success') item.success += 1
      if (kind === 'failed') item.failed += 1
      if (kind === 'running') item.running += 1
    })
    return Array.from(map.values()).slice(0, 8)
  }, [workflows])

  const maxTotal = Math.max(1, ...groups.map(group => group.total))

  return (
    <div className="tiny-bar-chart">
      {groups.map(item => (
        <div className="bar-item" key={item.group}>
          <div className="bar-track"><span style={{ height: `${Math.round((item.total / maxTotal) * 100)}%` }} /></div>
          <small>{item.group.slice(0, 5)}</small>
        </div>
      ))}
    </div>
  )
}

export default function Dashboard() {
  const [payload, setPayload] = useState(dashboardCache?.payload || null)
  const [health, setHealth] = useState(dashboardCache?.health || null)
  const [ping, setPing] = useState(dashboardCache?.ping || null)
  const [session, setSession] = useState(dashboardCache?.session || null)
  const [activeUsers, setActiveUsers] = useState(dashboardCache?.activeUsers || [])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(!dashboardCache)
  const [refreshing, setRefreshing] = useState(Boolean(dashboardCache))
  const [realtimeFallback, setRealtimeFallback] = useState(false)

  async function load({ silent = false } = {}) {
    if (silent) setRefreshing(true)
    else setLoading(true)

    setError(null)
    const [monitorResult, healthResult, pingResult, sessionResult, activeUsersResult] = await Promise.allSettled([
      api.monitor(),
      api.health(),
      api.snowflakePing(),
      api.session(),
      api.activeUsers()
    ])

    const monitorData = resolveSettled(monitorResult, dashboardCache?.payload || null)
    const healthData = resolveSettled(healthResult, dashboardCache?.health || null)
    const pingData = resolveSettled(pingResult, dashboardCache?.ping || null)
    const sessionData = resolveSettled(sessionResult, dashboardCache?.session || null)
    const activePayload = resolveSettled(activeUsersResult, null)
    const usersData = activePayload?.users || sessionData?.activeUsers || dashboardCache?.activeUsers || []

    dashboardCache = {
      payload: monitorData,
      health: healthData,
      ping: pingData,
      session: sessionData,
      activeUsers: usersData,
      cachedAt: new Date().toISOString()
    }

    setPayload(monitorData)
    setHealth(healthData)
    setPing(pingData)
    setSession(sessionData)
    setActiveUsers(usersData)

    const failed = [monitorResult, healthResult].filter(result => result.status === 'rejected')
    if (failed.length) setError(failed.map(result => result.reason?.message || String(result.reason)).join(' | '))

    setLoading(false)
    setRefreshing(false)
  }

  useEffect(() => {
    load({ silent: Boolean(dashboardCache) })
  }, [])

  useEffect(() => {
    const source = createKumoEventSource((event) => {
      const type = event?.type
      const data = event?.data || {}

      if (type === 'connected') {
        setRealtimeFallback(false)
        return
      }

      if (type === 'monitor_update') {
        setPayload(data)
        dashboardCache = { ...(dashboardCache || {}), payload: data, cachedAt: new Date().toISOString() }
        setLoading(false)
        setRefreshing(false)
        setRealtimeFallback(false)
        return
      }

      if (['workflow_run_requested', 'workflow_run_queued', 'workflow_run_status', 'workflow_run_failed'].includes(type)) {
        const patchedData = type === 'workflow_run_failed' ? { ...data, status: 'FAILED' } : data
        setPayload(previous => applyRealtimeRun(previous, patchedData))
      }
    }, () => {
      setRealtimeFallback(true)
    })

    if (!source) setRealtimeFallback(true)
    return () => source?.close()
  }, [])

  useEffect(() => {
    if (!realtimeFallback) return undefined

    const id = setInterval(() => load({ silent: true }), 30000)
    return () => clearInterval(id)
  }, [realtimeFallback])

  const workflows = payload?.workflows || []
  const summary = payload?.summary || buildSummary(workflows)
  const engine = payload?.engine || { status: 'UNKNOWN' }
  const successRate = percent(summary.success, summary.total)
  const failedWorkflows = workflows.filter(workflow => statusKind(workflow.lastStatus) === 'failed')
  const runningWorkflows = workflows.filter(workflow => ['running', 'queued'].includes(statusKind(workflow.lastStatus)))
  const recent = workflows.slice().sort((a, b) => String(b.lastStartTime || '').localeCompare(String(a.lastStartTime || ''))).slice(0, 6)

  const mockMode = Boolean(health?.mock)
  const snowflakeOk = Boolean(ping?.ok)
  const warehouse = ping?.snowflake?.WAREHOUSE_NAME || ping?.snowflake?.warehouse_name || 'Not selected'
  const role = session?.roleName || ping?.snowflake?.ROLE_NAME || ping?.snowflake?.role_name || 'Unknown role'
  const displayName = session?.displayName || session?.userName || 'KUMO user'
  const welcomeName = session?.firstName || firstWord(displayName)
  const callerRightsActive = Boolean(session?.callerRightsActive)
  const engineKind = statusKind(engine.status)
  const engineOk = ['success', 'running'].includes(engineKind)
  const cacheFresh = Boolean(payload?.generatedAt)

  return (
    <section className="page dashboard-page">
      <div className="page-hero dashboard-hero">
        <div>
          <p className="breadcrumb">Pages / Dashboard</p>
          <h1 className="page-heading">Dashboard</h1>
        </div>
        <div className="user-chip">
          <span className="user-avatar">{displayName.slice(0, 1).toUpperCase()}</span>
          <div><strong>{displayName}</strong><small>{role}</small></div>
        </div>
      </div>

      <div className={`connection-strip ${snowflakeOk ? 'success' : 'failed'}`}>
        {mockMode ? 'Mock mode' : snowflakeOk ? 'Snowflake connected' : 'Snowflake check failed'}
        {realtimeFallback && <span> · Realtime fallback polling active</span>}
      </div>

      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}
      {(loading || refreshing) && (
        <div className="alert info">
          {loading ? 'Updating dashboard...' : 'Refreshing dashboard...'} Collecting monitor status, Snowflake health and active user data.
        </div>
      )}

      <div className="dashboard-grid hero-grid">
        <div className="vision-card welcome-card">
          <span className="eyebrow">KUMO Monitor</span>
          <h2>Welcome back, {welcomeName}</h2>
          <p>Your workflow estate is being monitored in Snowpark Container Services. Your current Snowflake identity and role are shown below.</p>
          <div className="hero-status-row">
            <StatusBadge status={engine.status || 'UNKNOWN'} />
            <span>{summary.total || 0} workflows</span>
            <span>{role}</span>
            <span>{callerRightsActive ? 'Real user context' : 'Service context'}</span>
          </div>
        </div>
        <CurrentUserCard session={session} role={role} warehouse={warehouse} />
      </div>

      <div className="metrics-row">
        <MetricCard label="Workflow Success Rate" value={`${successRate}%`} delta={`${summary.success || 0}/${summary.total || 0} OK`} tone="success" icon="OK" footer="Latest run status" />
        <MetricCard label="Running / Queued" value={Number(summary.running || 0) + Number(summary.queued || 0)} delta={`${summary.running || 0} running, ${summary.queued || 0} queued`} tone="running" icon="RUN" footer="Active workload" />
        <MetricCard label="Failed Latest Runs" value={summary.failed || 0} tone={summary.failed ? 'failed' : 'success'} icon="ERR" footer="Needs attention" />
        <MetricCard label="Last Monitor Update" value={cacheFresh ? formatDateTime(payload.generatedAt) : '-'} tone={engineOk ? 'success' : 'failed'} icon="TIME" footer="Backend cache timestamp" />
      </div>

      <div className="dashboard-grid three">
        <div className="vision-card">
          <div className="card-heading"><div><h3>Health Checks</h3><p>Runtime and Snowflake session</p></div></div>
          <HealthCheck label="Backend" detail={health?.status || (health ? 'OK' : 'Unknown')} ok={Boolean(health)} />
          <HealthCheck label="Snowflake" detail={snowflakeOk ? `Warehouse ${warehouse}` : 'Ping failed'} ok={snowflakeOk} />
          <HealthCheck label="Engine" detail={engine.status || 'UNKNOWN'} ok={engineOk} tone={engineKind} />
        </div>

        <div className="vision-card">
          <div className="card-heading"><div><h3>Workflow Distribution</h3><p>Groups from current monitor payload</p></div></div>
          <TinyBarChart workflows={workflows} />
          <div className="mini-stat-row">
            <span>{summary.success || 0}<small>Success</small></span>
            <span>{summary.failed || 0}<small>Failed</small></span>
            <span>{summary.running || 0}<small>Running</small></span>
            <span>{summary.queued || 0}<small>Queued</small></span>
          </div>
        </div>

        <div className="vision-card">
          <div className="card-heading"><div><h3>Active Workflows</h3><p>Running and queued jobs</p></div></div>
          {runningWorkflows.length === 0 ? (
            <div className="soft-empty">No active workflows right now.</div>
          ) : runningWorkflows.slice(0, 5).map(workflow => (
            <div className="running-card" key={workflow.workflowId}>
              <div><strong>{workflow.workflowName}</strong><StatusBadge status={workflow.lastStatus || 'UNKNOWN'} /></div>
              <ProgressBar progress={workflow.progress} status={workflow.lastStatus} />
            </div>
          ))}
        </div>
      </div>

      <div className="dashboard-grid two">
        <div className="vision-card">
          <div className="card-heading"><div><h3>Recent Activity</h3><p>Latest workflow runs</p></div></div>
          {recent.length ? recent.map(workflow => <WorkflowActivity workflow={workflow} key={`${workflow.workflowId}-${workflow.lastRunId || ''}`} />) : <div className="soft-empty">No recent workflow activity.</div>}
        </div>

        <div className="vision-card">
          <div className="card-heading"><div><h3>Attention</h3><p>Failed latest runs</p></div></div>
          {failedWorkflows.length === 0 ? (
            <div className="soft-empty success">No failed workflows in latest status.</div>
          ) : failedWorkflows.slice(0, 5).map(workflow => (
            <div className="attention-row" key={workflow.workflowId}>
              <span>ERR</span>
              <div><strong>{workflow.workflowName}</strong><small>{workflow.workflowGroup || 'Ungrouped'} · {formatDateTime(workflow.lastStartTime)}</small></div>
            </div>
          ))}
        </div>
      </div>

      <ActiveUsersCard users={activeUsers} currentUserName={session?.userName} />
    </section>
  )
}
