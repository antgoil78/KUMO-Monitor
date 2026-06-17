import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
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

export default function Dashboard() {
  const [payload, setPayload] = useState(null)
  const [health, setHealth] = useState(null)
  const [ping, setPing] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  async function load() {
    setError(null)
    const [monitorResult, healthResult, pingResult] = await Promise.allSettled([
      api.monitor(),
      api.health(),
      api.snowflakePing()
    ])

    const monitorData = resolveSettled(monitorResult)
    const healthData = resolveSettled(healthResult)
    const pingData = resolveSettled(pingResult)

    setPayload(monitorData)
    setHealth(healthData)
    setPing(pingData)

    const failed = [monitorResult, healthResult].filter(r => r.status === 'rejected')
    if (failed.length) setError(failed.map(r => r.reason?.message || String(r.reason)).join(' | '))
    setLoading(false)
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 10000)
    return () => clearInterval(id)
  }, [])

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
  const role = ping?.snowflake?.ROLE_NAME || ping?.snowflake?.role_name || 'Unknown role'
  const engineKind = statusKind(engine.status)
  const engineOk = ['success', 'running'].includes(engineKind)
  const cacheFresh = Boolean(payload?.generatedAt)

  return (
    <section className="page dashboard-page">
      <div className="dashboard-topbar">
        <div>
          <p className="breadcrumb">Pages / Dashboard</p>
          <h1>Dashboard</h1>
        </div>
        <div className="topbar-status">
          <span className={`topbar-dot ${snowflakeOk ? 'success' : mockMode ? 'queued' : 'failed'}`} />
          <span>{mockMode ? 'Mock mode' : snowflakeOk ? 'Snowflake connected' : 'Snowflake degraded'}</span>
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
          label="Backend refresh"
          value={`${health?.refreshSeconds || payload?.refreshIntervalMs / 1000 || 5}s`}
          delta="Live polling"
          tone="queued"
          icon="↻"
          footer={`Updated ${formatDateTime(payload?.generatedAt)}`}
        />
      </div>

      <div className="dashboard-layout">
        <div className="vision-card welcome-card">
          <div className="welcome-content">
            <span className="eyebrow">KUMO Monitor</span>
            <h2>Welcome back, Andreas</h2>
            <p>
              Your workflow estate is being monitored in Snowpark Container Services.
              Start with the health checks, then jump into Monitor when a run needs attention.
            </p>
            <div className="welcome-actions">
              <span className={`glass-pill ${engineOk ? 'success' : 'failed'}`}>Engine {engine.status || 'UNKNOWN'}</span>
              <span className="glass-pill">{summary.total || 0} workflows</span>
              <span className="glass-pill">{role}</span>
            </div>
          </div>
          <div className="orb-stage" aria-hidden="true">
            <div className="orb orb-main" />
            <div className="orb-ring ring-one" />
            <div className="orb-ring ring-two" />
          </div>
        </div>

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
            <HealthCheck label="Monitor cache" detail={cacheFresh ? `Fresh ${formatDateTime(payload?.generatedAt)}` : 'No data'} ok={cacheFresh} />
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
    </section>
  )
}
