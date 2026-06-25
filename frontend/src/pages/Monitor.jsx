import { useEffect, useMemo, useRef, useState } from 'react'
import { api, createKumoEventSource } from '../api.js'
import StatusBadge, { isWorkflowBusy, statusKind } from '../components/StatusBadge.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'

const statusOptions = [
  { value: '', label: 'All statuses' },
  { value: 'SUCCESS', label: 'Success' },
  { value: 'FAILED', label: 'Failed' },
  { value: 'RUNNING', label: 'Running' },
  { value: 'INITIATING', label: 'Initiating' },
  { value: 'QUEUED', label: 'Queued' },
  { value: '-', label: 'No status' }
]

const activeRunStatuses = new Set(['INITIATING', 'RUNNING', 'IN_PROGRESS', 'EXECUTING', 'QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED', 'STARTING'])
const runningRunStatuses = new Set(['RUNNING', 'IN_PROGRESS', 'EXECUTING', 'STARTING'])
const terminalRunStatuses = new Set(['SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK', 'FAILED', 'FAILURE', 'ERROR', 'CANCELLED', 'CANCELED', 'SKIPPED'])
const statusRank = {
  INITIATING: 10,
  REQUESTED: 20,
  PENDING: 20,
  SCHEDULED: 20,
  QUEUED: 20,
  STARTING: 30,
  RUNNING: 30,
  IN_PROGRESS: 30,
  EXECUTING: 30,
  SUCCESS: 40,
  SUCCEEDED: 40,
  COMPLETED: 40,
  OK: 40,
  FAILED: 40,
  FAILURE: 40,
  ERROR: 40,
  CANCELLED: 40,
  CANCELED: 40,
  SKIPPED: 40
}

function normalizeStatus(status, fallback = 'INITIATING') {
  const value = String(status || '').trim().toUpperCase()
  return value || fallback
}

function liveRunFromEvent(data) {
  const workflowId = String(data?.workflowId || data?.lock?.workflowId || '')
  if (!workflowId) return null
  return {
    lockId: data?.lock?.lockId || '',
    workflowId,
    workflowName: data?.workflowName || data?.lock?.workflowName || workflowId,
    runId: data?.runId || data?.lock?.runId || '',
    status: normalizeStatus(data?.status || data?.lock?.status),
    requestedAt: data?.requestedAt || data?.lock?.requestedAt || new Date().toISOString(),
    requestedBy: data?.requestedBy || data?.lock?.requestedBy || data?.actor?.displayName || data?.actor?.userName || '',
    lastStartTime: data?.lastStartTime || data?.lock?.lastStartTime || null,
    lastEndTime: data?.lastEndTime || data?.lock?.lastEndTime || null,
    message: data?.message || data?.lock?.message || '',
    updatedAt: Date.now(),
    ...(data?.lock || {})
  }
}

function upsertLock(locks, lock) {
  if (!lock?.workflowId) return locks || []
  const next = new Map((locks || []).map(item => [item.workflowId, item]))
  const previous = next.get(lock.workflowId) || {}
  const previousStatus = normalizeStatus(previous.status, '')
  const nextStatus = normalizeStatus(lock.status, '')
  const previousRun = String(previous.runId || '')
  const nextRun = String(lock.runId || previousRun || '')
  const isDowngrade = previousStatus && nextStatus && previousRun === nextRun && (statusRank[previousStatus] || 0) > (statusRank[nextStatus] || 0)
  const merged = isDowngrade
    ? { ...lock, ...previous, status: previousStatus }
    : { ...previous, ...lock, status: lock.status }
  next.set(lock.workflowId, merged)
  return Array.from(next.values())
}

function mergeLockSnapshot(previousLocks, incomingLocks) {
  const incoming = incomingLocks || []
  if (!incoming.length) return []
  const previousByWorkflow = new Map((previousLocks || []).map(item => [item.workflowId, item]))
  return incoming.reduce((out, lock) => {
    const previous = previousByWorkflow.get(lock.workflowId)
    return upsertLock(out, previous ? upsertLock([previous], lock)[0] : lock)
  }, [])
}

function reconcileLocksFromWorkflows(locks, workflows) {
  if (!locks?.length) return []
  const byWorkflow = new Map((workflows || []).map(wf => [String(wf.workflowId), wf]))
  return locks.reduce((out, lock) => {
    const workflow = byWorkflow.get(String(lock.workflowId))
    if (!workflow) {
      out.push(lock)
      return out
    }

    if (workflow.runLock) {
      out.push(upsertLock([lock], workflow.runLock)[0])
      return out
    }

    const sameRun = lock.runId && String(workflow.lastRunId || '') === String(lock.runId)
    const status = normalizeStatus(workflow.lastStatus, '-')
    if (sameRun && terminalRunStatuses.has(status)) {
      return out
    }

    out.push(lock)
    return out
  }, [])
}

function SummaryItem({ tone, icon, label, value }) {
  return (
    <span className={`summary-item ${tone || ''}`}>
      <span className="summary-icon">{icon}</span>
      <strong>{value}</strong>
      <span>{label}</span>
    </span>
  )
}

function RunningProgress({ workflow }) {
  const s = String(workflow.lastStatus || '').toUpperCase()
  const isBusy = ['INITIATING', 'RUNNING', 'IN_PROGRESS', 'EXECUTING', 'QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED'].includes(s)

  if (!isBusy && !workflow.progress) return null

  return (
    <div className="inline-progress">
      <ProgressBar progress={workflow.progress} status={workflow.lastStatus} />
    </div>
  )
}

function useOutsideClick(ref, onClose) {
  useEffect(() => {
    function handler(event) {
      if (!ref.current || ref.current.contains(event.target)) return
      onClose()
    }
    function esc(event) {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('keydown', esc)
    }
  }, [ref, onClose])
}

function RowActions({ workflow, isOpen, onOpen, onClose, onAction, disabledRun }) {
  const ref = useRef(null)
  useOutsideClick(ref, onClose)

  const wfEnabled = Boolean(workflow.workflowEnabled)
  const taskEnabled = Boolean(workflow.taskEnabled)
  const isDbt = String(workflow.workflowType || '').toUpperCase() === 'DBT'

  async function click(action) {
    onClose()
    await onAction(action, workflow)
  }

  return (
    <div className="row-menu-control" ref={ref}>
      <button
        className={`row-menu-trigger ${isOpen ? 'open' : ''}`}
        aria-label={`Actions for ${workflow.workflowName}`}
        onClick={() => (isOpen ? onClose() : onOpen())}
      >
        <span>⋮</span><span>⌄</span>
      </button>
      {isOpen && (
        <div className="row-menu-panel vision-popover">
          <button disabled={!wfEnabled || disabledRun} onClick={() => click('run')}>▶ Run workflow</button>
          {isDbt && <button onClick={() => click('dag')}>⌘ Show DAG run</button>}
          <button onClick={() => click('history')}>◷ History</button>
          <button onClick={() => click('edit')}>✎ Edit</button>
          <div className="menu-divider" />
          <button onClick={() => click('toggle-workflow')}>{wfEnabled ? 'Ⅱ Disable workflow' : '▶ Enable workflow'}</button>
          <button disabled={!wfEnabled && !taskEnabled} onClick={() => click('toggle-schedule')}>{taskEnabled ? '◴ Disable schedule' : '◷ Enable schedule'}</button>
          {workflow.lastRunId && <div className="run-id-note">Run ID:<code>{workflow.lastRunId}</code></div>}
        </div>
      )}
    </div>
  )
}

function WorkflowRow({ workflow, nowMs, onManage, pendingRun }) {
  const disabled = !workflow.workflowEnabled
  const depth = Number(workflow.indent || 0)
  const type = String(workflow.workflowType || 'DBT').toUpperCase()
  const isRoot = depth === 0
  const view = workflow
  const runLock = view.runLock || null
  const busy = isWorkflowBusy(view.lastStatus) || Boolean(view.runLocked)

  return (
    <tr className={`${disabled ? 'disabled-row' : ''} ${isRoot ? 'root-row' : 'child-row'} depth-row-${Math.min(depth, 6)} ${busy ? 'busy-row' : ''}`}>
      <td className="workflow-cell">
        <div className={`workflow-tree depth-${Math.min(depth, 6)}`} style={{ '--depth': depth }}>
          {depth > 0 && <span className="tree-branch" aria-hidden="true" />}
          <span className={`workflow-title ${isRoot ? 'root' : 'child'}`}>{workflow.workflowName}</span>
          <span className={`type-chip ${type.toLowerCase()}`}>{type}</span>
        </div>
      </td>
      <td className="row-actions">
        <button
          className="row-manage-button"
          aria-label={`Manage ${workflow.workflowName}`}
          title={`Manage ${workflow.workflowName}`}
          onClick={() => onManage(view)}
        >
          <span className="manage-dot" />
          <span>Manage</span>
        </button>
      </td>
      <td className="status-cell">
        <StatusBadge status={view.lastStatus} />
        {view.runLocked && <span className="run-lock-note">Locked by {view.runLock?.requestedBy || 'another user'}</span>}
      </td>
      <td className="muted-cell">{formatDateTime(view.lastStartTime)}</td>
      <td className="duration-cell">
        <span>{elapsedDuration(view.lastStartTime, view.lastEndTime, view.lastStatus, nowMs)}</span>
        <RunningProgress workflow={view} />
      </td>
      <td className="schedule-cell">
        {workflow.taskEnabled ? <><code>{workflow.scheduleCron || '-'}</code><span>{workflow.scheduleTimezone || 'UTC'}</span></> : <span className="muted-dash">—</span>}
      </td>
      <td className="muted-cell">{workflow.taskEnabled ? formatDateTime(workflow.nextRunTime) : '—'}</td>
    </tr>
  )
}

function Modal({ title, subtitle, onClose, children, wide = false }) {
  useEffect(() => {
    function onKey(event) {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className={`vision-modal ${wide ? 'wide' : ''}`}>
        <div className="modal-header">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        {children}
      </div>
    </div>
  )
}

function MultiSelect({ label, options, value, onChange }) {
  const selected = new Set(value || [])
  function toggle(id) {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    onChange(Array.from(next))
  }
  return (
    <div className="form-field">
      <label>{label}</label>
      <div className="multi-list">
        {options.length === 0 ? <span className="muted-dash">No workflows available</span> : options.map(option => (
          <label className="multi-item" key={option.workflowId}>
            <input type="checkbox" checked={selected.has(option.workflowId)} onChange={() => toggle(option.workflowId)} />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </div>
  )
}

function EditModal({ workflowId, onClose, onSaved, notify }) {
  const [detail, setDetail] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer = null

    async function loadDetail() {
      setError(null)
      setDetail(null)
      timer = window.setTimeout(() => {
        if (!cancelled) {
          setError('Workflow details are still loading. This is slower than expected; check backend logs for /api/workflows/' + workflowId + ' or try again.')
        }
      }, 35000)

      try {
        const data = await api.workflowDetail(workflowId, { timeoutMs: 60000 })
        if (!cancelled) {
          setDetail(data)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      } finally {
        if (timer) window.clearTimeout(timer)
      }
    }

    loadDetail()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [workflowId])

  function patch(field, value) {
    setDetail(prev => ({ ...prev, [field]: value }))
  }
  function patchNotif(field, value) {
    setDetail(prev => ({ ...prev, notifications: { ...(prev.notifications || {}), [field]: value } }))
  }

  function closeAndRefresh(message) {
    notify(message)
    onClose()

    // Refresh the monitor after the modal has closed. Keeping this non-blocking
    // prevents the modal from staying open while Snowflake refreshes data.
    Promise.resolve(onSaved()).catch(err => {
      notify(`Refresh failed after update: ${err.message}`)
    })
  }

  async function save() {
    setSaving(true)
    setError(null)
    let completed = false
    try {
      await api.updateWorkflow(workflowId, detail)
      completed = true
      closeAndRefresh('Workflow saved')
    } catch (err) {
      setError(err.message)
    } finally {
      if (!completed) setSaving(false)
    }
  }

  async function clone() {
    setSaving(true)
    setError(null)
    let completed = false
    try {
      await api.cloneWorkflow(workflowId)
      completed = true
      closeAndRefresh('Workflow cloned and disabled')
    } catch (err) {
      setError(err.message)
    } finally {
      if (!completed) setSaving(false)
    }
  }

  async function remove() {
    setSaving(true)
    setError(null)
    let completed = false
    try {
      await api.deleteWorkflow(workflowId)
      completed = true
      closeAndRefresh('Workflow deleted')
    } catch (err) {
      setError(err.message)
    } finally {
      if (!completed) setSaving(false)
    }
  }

  return (
    <Modal title="Edit workflow" subtitle={detail?.workflowName || workflowId} onClose={onClose} wide>
      {!detail && !error && <div className="empty-state">Loading workflow...</div>}
      {error && <div className="alert error">{error}</div>}
      {error && !detail && (
        <div className="modal-actions left">
          <button className="button primary" onClick={() => { setError(null); setDetail(null); api.workflowDetail(workflowId, { timeoutMs: 60000 }).then(setDetail).catch(err => setError(err.message)) }}>Retry loading workflow</button>
          <button className="button muted" onClick={onClose}>Close</button>
        </div>
      )}
      {detail && (
        <div className="edit-form">
          <div className="form-grid two">
            <div className="form-field"><label>Name</label><input value={detail.workflowName || ''} onChange={e => patch('workflowName', e.target.value)} /></div>
            <div className="form-field"><label>Group</label><input value={detail.workflowGroup || ''} onChange={e => patch('workflowGroup', e.target.value)} /></div>
            <div className="form-field"><label>Type</label><select value={detail.workflowType || 'DBT'} onChange={e => patch('workflowType', e.target.value)}><option>DBT</option><option>SQL</option></select></div>
            <div className="toggle-row"><label><input type="checkbox" checked={Boolean(detail.workflowEnabled)} onChange={e => patch('workflowEnabled', e.target.checked)} /> Workflow enabled</label><label><input type="checkbox" checked={Boolean(detail.taskEnabled)} onChange={e => patch('taskEnabled', e.target.checked)} /> Schedule enabled</label></div>
          </div>
          <div className="form-field"><label>Description</label><textarea rows="2" value={detail.description || ''} onChange={e => patch('description', e.target.value)} /></div>

          {String(detail.workflowType).toUpperCase() === 'DBT' ? (
            <>
              <div className="form-field"><label>DBT Command</label><textarea rows="3" value={detail.dbtCommand || ''} onChange={e => patch('dbtCommand', e.target.value)} /></div>
              <div className="form-grid two">
                <div className="form-field"><label>DBT Project FQN</label><input value={detail.dbtProjectFqn || ''} onChange={e => patch('dbtProjectFqn', e.target.value)} /></div>
                <div className="form-field"><label>DBT Target</label><input value={detail.dbtTarget || ''} onChange={e => patch('dbtTarget', e.target.value)} /></div>
              </div>
            </>
          ) : (
            <div className="form-field"><label>SQL Command</label><textarea rows="5" value={detail.sqlCommand || ''} onChange={e => patch('sqlCommand', e.target.value)} /></div>
          )}

          <div className="form-grid two">
            <div className="form-field"><label>Cron</label><input value={detail.scheduleCron || ''} onChange={e => patch('scheduleCron', e.target.value)} /></div>
            <div className="form-field"><label>Timezone</label><input value={detail.scheduleTimezone || 'UTC'} onChange={e => patch('scheduleTimezone', e.target.value)} /></div>
          </div>

          <div className="form-grid two">
            <MultiSelect label="On Success" options={detail.workflowOptions || []} value={detail.onSuccess || []} onChange={v => patch('onSuccess', v)} />
            <MultiSelect label="On Fail" options={detail.workflowOptions || []} value={detail.onFail || []} onChange={v => patch('onFail', v)} />
          </div>

          <details className="advanced-section">
            <summary>Notifications</summary>
            <div className="form-grid two">
              <div className="toggle-row vertical"><label><input type="checkbox" checked={Boolean(detail.notifications?.onFailEmail)} onChange={e => patchNotif('onFailEmail', e.target.checked)} /> Email on failure</label><label><input type="checkbox" checked={Boolean(detail.notifications?.onSuccessEmail)} onChange={e => patchNotif('onSuccessEmail', e.target.checked)} /> Email on success</label></div>
              <div className="form-field"><label>Email integration</label><input value={detail.notifications?.emailIntegration || ''} onChange={e => patchNotif('emailIntegration', e.target.value)} /></div>
              <div className="form-field"><label>Fail group</label><input list="email-groups" value={detail.notifications?.failGroup || ''} onChange={e => patchNotif('failGroup', e.target.value)} /></div>
              <div className="form-field"><label>Success group</label><input list="email-groups" value={detail.notifications?.successGroup || ''} onChange={e => patchNotif('successGroup', e.target.value)} /></div>
              <div className="form-field"><label>Environment</label><input value={detail.notifications?.environment || ''} onChange={e => patchNotif('environment', e.target.value)} /></div>
            </div>
            <datalist id="email-groups">{(detail.emailGroups || []).map(g => <option key={g} value={g} />)}</datalist>
          </details>

          {confirmDelete && <div className="alert warning">Delete this workflow and related queue/history/task rows? This cannot be undone.</div>}
          <div className="modal-actions">
            <button className="button primary" disabled={saving} onClick={save}>Save</button>
            <button className="button" disabled={saving} onClick={clone}>Clone</button>
            {!confirmDelete ? <button className="button danger" disabled={saving} onClick={() => setConfirmDelete(true)}>Delete</button> : <button className="button danger" disabled={saving} onClick={remove}>Confirm delete</button>}
            <button className="button muted" disabled={saving} onClick={onClose}>Cancel</button>
          </div>
        </div>
      )}
    </Modal>
  )
}

function HistoryModal({ workflow, onClose }) {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let cancelled = false
    api.workflowHistory(workflow.workflowId).then(data => {
      if (!cancelled) setRows(data.rows || [])
    }).catch(err => !cancelled && setError(err.message))
    return () => { cancelled = true }
  }, [workflow.workflowId])
  return (
    <Modal title="Workflow history" subtitle={workflow.workflowName} onClose={onClose} wide>
      {error && <div className="alert error">{error}</div>}
      {!rows && !error && <div className="empty-state">Loading history...</div>}
      {rows && <div className="modal-table-wrap"><table className="workflow-table compact"><thead><tr><th>Run ID</th><th>Status</th><th>Requested</th><th>Start</th><th>End</th><th>Error</th></tr></thead><tbody>{rows.map(row => <tr key={row.RUN_ID}><td><code>{row.RUN_ID}</code></td><td><StatusBadge status={row.STATUS} /></td><td>{formatDateTime(row.REQUESTED_AT)}</td><td>{formatDateTime(row.START_TIME)}</td><td>{formatDateTime(row.END_TIME)}</td><td>{String(row.ERROR_MESSAGE || '').slice(0, 120)}</td></tr>)}</tbody></table></div>}
    </Modal>
  )
}

function DagModal({ workflow, onClose }) {
  const [dag, setDag] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let cancelled = false
    api.workflowDag(workflow.workflowId).then(data => !cancelled && setDag(data)).catch(err => !cancelled && setError(err.message))
    return () => { cancelled = true }
  }, [workflow.workflowId])
  const nodes = dag?.nodes || []
  const done = nodes.filter(n => ['DONE', 'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK'].includes(String(n.status).toUpperCase())).length
  const failed = nodes.filter(n => ['ERROR', 'FAILED', 'FAILURE'].includes(String(n.status).toUpperCase())).length
  const percent = nodes.length ? Math.round((done / nodes.length) * 100) : 0
  return (
    <Modal title="DAG run" subtitle={workflow.workflowName} onClose={onClose} wide>
      {error && <div className="alert error">{error}</div>}
      {!dag && !error && <div className="empty-state">Loading DAG...</div>}
      {dag && <>
        <div className="dag-summary"><StatusBadge status={dag.run?.STATUS || '—'} /><span>Run ID <code>{dag.run?.RUN_ID || '—'}</code></span><span>{done}/{nodes.length} completed</span>{failed > 0 && <span className="failed-text">{failed} failed</span>}</div>
        <div className="dag-progress"><ProgressBar progress={{ percent, total: nodes.length, done, failed }} status={dag.run?.STATUS} /></div>
        <div className="dag-node-grid">{nodes.length ? nodes.map(node => <div className={`dag-node ${statusKind(node.status)}`} key={node.id} title={node.id}>{node.label}</div>) : <div className="soft-empty">No execution progress data for this run.</div>}</div>
        {dag.errors?.length > 0 && <div className="modal-table-wrap"><table className="workflow-table compact"><thead><tr><th>Time</th><th>Model</th><th>Error</th></tr></thead><tbody>{dag.errors.map((err, idx) => <tr key={`${err.ORIGIN}-${idx}`}><td>{formatDateTime(err.LOG_DTTM)}</td><td>{err.ORIGIN}</td><td>{String(err.MESSAGE || '').slice(0, 240)}</td></tr>)}</tbody></table></div>}
      </>}
    </Modal>
  )
}

function ActionsModal({ workflow, onClose, onAction, pendingRun }) {
  const view = pendingRun ? { ...workflow, lastStatus: pendingRun.status || 'INITIATING', lastRunId: pendingRun.runId || workflow.lastRunId } : workflow
  const wfEnabled = Boolean(view.workflowEnabled)
  const taskEnabled = Boolean(view.taskEnabled)
  const isDbt = String(view.workflowType || '').toUpperCase() === 'DBT'
  const runLock = view.runLock || null
  const busy = isWorkflowBusy(view.lastStatus) || Boolean(view.runLocked)

  async function choose(action) {
    await onAction(action, view)
  }

  return (
    <Modal title="Workflow actions" subtitle={view.workflowName} onClose={onClose}>
      <div className="action-modal-head">
        <div>
          <span className="modal-eyebrow">Current status</span>
          <StatusBadge status={view.lastStatus} />
        </div>
        {view.lastRunId && <code>{view.lastRunId}</code>}
      </div>

      {runLock && <div className="alert info compact">This workflow is locked for a pending run. Run ID: <code>{runLock.runId || 'pending'}</code></div>}

      <div className="action-grid">
        <button className="action-tile primary" disabled={!wfEnabled || busy} onClick={() => choose('run')}>
          <span className="action-icon">▶</span>
          <strong>{busy ? 'Workflow active' : 'Run workflow'}</strong>
          <small>{runLock ? `Requested by ${runLock.requestedBy || 'another user'}` : (busy ? 'Run is disabled while initiating, queued or running.' : 'Create a manual run request.')}</small>
        </button>
        {isDbt && (
          <button className="action-tile" onClick={() => choose('dag')}>
            <span className="action-icon">⌘</span>
            <strong>Show DAG run</strong>
            <small>Open latest DBT execution progress.</small>
          </button>
        )}
        <button className="action-tile" onClick={() => choose('history')}>
          <span className="action-icon">◷</span>
          <strong>History</strong>
          <small>View recent workflow executions.</small>
        </button>
        <button className="action-tile" onClick={() => choose('edit')}>
          <span className="action-icon">✎</span>
          <strong>Edit workflow</strong>
          <small>Change metadata, schedule, dependencies and notifications.</small>
        </button>
        <button className="action-tile" onClick={() => choose('toggle-workflow')}>
          <span className="action-icon">{wfEnabled ? 'Ⅱ' : '▶'}</span>
          <strong>{wfEnabled ? 'Disable workflow' : 'Enable workflow'}</strong>
          <small>{wfEnabled ? 'Also prevents future manual runs.' : 'Allow workflow runs again.'}</small>
        </button>
        <button className="action-tile" disabled={!wfEnabled && !taskEnabled} onClick={() => choose('toggle-schedule')}>
          <span className="action-icon">◴</span>
          <strong>{taskEnabled ? 'Disable schedule' : 'Enable schedule'}</strong>
          <small>{taskEnabled ? 'Keep workflow but stop timed triggers.' : 'Resume scheduled execution.'}</small>
        </button>
      </div>
    </Modal>
  )
}

export default function Monitor() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [actionMessage, setActionMessage] = useState(null)
  const [openMenuId, setOpenMenuId] = useState(null)
  const [pendingRuns, setPendingRuns] = useState({})
  const [globalLocks, setGlobalLocks] = useState([])
  const [modal, setModal] = useState(null)

  async function load(force = false) {
    try {
      setError(null)
      const data = force ? await api.refreshMonitor() : await api.monitor()
      setPayload(data)
      setGlobalLocks(prev => reconcileLocksFromWorkflows(prev, data.workflows || []))
      setPendingRuns(prev => {
        const next = { ...prev }
        const seen = new Set((data.workflows || []).map(wf => wf.workflowId))
        const now = Date.now()

        for (const wf of data.workflows || []) {
          const pending = next[wf.workflowId]
          if (!pending) continue

          const status = normalizeStatus(wf.lastStatus, '-')
          const pendingStatus = normalizeStatus(pending.status, '')
          const pendingIsAhead = pending.runId && pending.runId !== 'pending' &&
            String(wf.lastRunId || '') === String(pending.runId) &&
            (statusRank[pendingStatus] || 0) > (statusRank[status] || 0)
          if (pendingIsAhead) continue

          const actualBusy = activeRunStatuses.has(status)
          const runVisible = pending.runId && pending.runId !== 'pending' && String(wf.lastRunId || '') === String(pending.runId)
          const expired = now - Number(pending.startedAt || now) > 120000

          if (actualBusy || (runVisible && terminalRunStatuses.has(status)) || expired) {
            delete next[wf.workflowId]
          }
        }

        for (const workflowId of Object.keys(next)) {
          if (!seen.has(workflowId)) delete next[workflowId]
        }

        return next
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function loadLocks() {
    try {
      const data = await api.workflowRunLocks()
      setGlobalLocks(prev => mergeLockSnapshot(prev, data.locks || []))
    } catch (err) {
      // Do not block the monitor page if the lock poll has a transient failure.
      // The normal monitor payload still contains persisted workflow status.
      console.warn('Could not refresh workflow run locks', err)
    }
  }

  function applyLiveRunUpdate(data) {
    const liveRun = liveRunFromEvent(data)
    if (!liveRun) return
    if (terminalRunStatuses.has(normalizeStatus(liveRun.status, '-'))) {
      setGlobalLocks(prev => (prev || []).filter(lock => lock.workflowId !== liveRun.workflowId))
    } else {
      setGlobalLocks(prev => upsertLock(prev, liveRun))
    }
    setPendingRuns(prev => ({
      ...prev,
      [liveRun.workflowId]: {
        ...(prev[liveRun.workflowId] || {}),
        startedAt: prev[liveRun.workflowId]?.startedAt || Date.now(),
        runId: liveRun.runId || prev[liveRun.workflowId]?.runId || 'pending',
        status: liveRun.status || prev[liveRun.workflowId]?.status || 'INITIATING',
        lastStartTime: liveRun.lastStartTime || prev[liveRun.workflowId]?.lastStartTime || null,
        lastEndTime: liveRun.lastEndTime || prev[liveRun.workflowId]?.lastEndTime || null
      }
    }))
  }

  async function loadRealtimeState() {
    try {
      const data = await api.realtimeState()
      setGlobalLocks(prev => mergeLockSnapshot(prev, data.locks || []))
      for (const event of data.events || []) {
        applyLiveRunUpdate(event)
      }
    } catch (err) {
      console.warn('Could not refresh realtime state', err)
    }
  }

  useEffect(() => { load(false); loadLocks() }, [])
  useEffect(() => {
    const source = createKumoEventSource((event) => {
      const type = event?.type
      const data = event?.data || {}
      if (type === 'monitor_update') {
        setPayload(data)
        setGlobalLocks(prev => reconcileLocksFromWorkflows(prev, data.workflows || []))
        setPendingRuns(prev => {
          const next = { ...prev }
          for (const wf of data.workflows || []) {
            const pending = next[wf.workflowId]
            if (!pending) continue
            const status = normalizeStatus(wf.lastStatus, '-')
            const pendingStatus = normalizeStatus(pending.status, '')
            const sameRun = pending.runId && pending.runId !== 'pending' && String(wf.lastRunId || '') === String(pending.runId)
            if (sameRun && (statusRank[pendingStatus] || 0) > (statusRank[status] || 0)) {
              continue
            }
            if (activeRunStatuses.has(status) || (sameRun && terminalRunStatuses.has(status))) {
              delete next[wf.workflowId]
            }
          }
          return next
        })
        setLoading(false)
        return
      }

      if (['workflow_run_requested', 'workflow_run_queued', 'workflow_run_status'].includes(type)) {
        applyLiveRunUpdate(data)
        return
      }

      if (type === 'workflow_run_failed') {
        const liveRun = liveRunFromEvent({ ...data, status: 'FAILED' })
        if (!liveRun) return
        setGlobalLocks(prev => (prev || []).filter(lock => lock.workflowId !== liveRun.workflowId))
        setPendingRuns(prev => {
          const next = { ...prev }
          delete next[liveRun.workflowId]
          return next
        })
        notify(`Run request failed for ${liveRun.workflowName}: ${liveRun.error || data.error || 'Unknown error'}`)
      }
    }, () => {})
    return () => source?.close()
  }, [])
  useEffect(() => {
    const id = setInterval(() => loadRealtimeState(), 1500)
    return () => clearInterval(id)
  }, [])
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const workflows = payload?.workflows || []
  const lockByWorkflow = useMemo(() => {
    const map = {}
    for (const lock of globalLocks || []) {
      if (lock?.workflowId) map[lock.workflowId] = lock
    }
    return map
  }, [globalLocks])

  const workflowsWithPending = workflows.map(w => {
    const lock = lockByWorkflow[w.workflowId]
    let view = w

    if (lock) {
      const actualStatus = normalizeStatus(w.lastStatus, '-')
      const sameRun = lock.runId && String(w.lastRunId || '') === String(lock.runId)
      const actualRunning = sameRun && runningRunStatuses.has(actualStatus)
      view = {
        ...w,
        runLocked: true,
        runLock: lock,
        lastStatus: actualRunning ? w.lastStatus : (lock.status || 'QUEUED'),
        lastRunId: lock.runId || w.lastRunId,
        lastStartTime: actualRunning ? (lock.lastStartTime || w.lastStartTime) : (lock.lastStartTime || null),
        lastEndTime: actualRunning ? (lock.lastEndTime || w.lastEndTime) : null,
        lastRequestedAt: lock.requestedAt || w.lastRequestedAt,
        lastRequestedBy: lock.requestedBy || w.lastRequestedBy,
        progress: actualRunning ? (w.progress || { percent: null }) : { percent: null }
      }
    }

    const pending = pendingRuns[w.workflowId]
    if (!pending) return view
    const pendingStatus = normalizeStatus(pending.status, 'INITIATING')
    const pendingTerminal = terminalRunStatuses.has(pendingStatus)
    const viewStatus = normalizeStatus(view.lastStatus, '-')
    const pendingRunId = String(pending.runId || '')
    const viewRunId = String(view.lastRunId || view.runLock?.runId || '')
    const pendingSameRun = pendingRunId && pendingRunId !== 'pending' && viewRunId === pendingRunId
    const pendingIsAhead = pendingSameRun && (statusRank[pendingStatus] || 0) > (statusRank[viewStatus] || 0)
    if (view.runLocked && !pendingIsAhead) return view
    const actualBusy = isWorkflowBusy(view.lastStatus)
    const expired = Date.now() - Number(pending.startedAt || Date.now()) > 120000
    if (!pendingTerminal && ((actualBusy && !pendingIsAhead) || expired)) return view
    return {
      ...view,
      runLocked: pendingTerminal ? false : view.runLocked,
      runLock: pendingTerminal ? null : view.runLock,
      lastStatus: pendingStatus,
      lastRunId: pending.runId || view.lastRunId,
      lastStartTime: pending.lastStartTime || (pendingTerminal ? view.lastStartTime : null),
      lastEndTime: pending.lastEndTime || (pendingTerminal ? view.lastEndTime : null),
      progress: pendingTerminal ? null : { percent: null }
    }
  })
  const summary = useMemo(() => {
    const total = workflowsWithPending.length
    const success = workflowsWithPending.filter(w => statusKind(w.lastStatus) === 'success').length
    const failed = workflowsWithPending.filter(w => statusKind(w.lastStatus) === 'failed').length
    const running = workflowsWithPending.filter(w => statusKind(w.lastStatus) === 'running').length
    const queued = workflowsWithPending.filter(w => statusKind(w.lastStatus) === 'queued').length
    return { total, success, failed, running, queued }
  }, [workflowsWithPending])
  const engine = payload?.engine || { status: 'UNKNOWN' }

  const filtered = useMemo(() => {
    const f = filter.trim().toLowerCase()
    return workflowsWithPending.filter(w => {
      const matchesName = !f || [w.workflowName, w.workflowGroup, w.workflowType, w.lastRunId].some(v => String(v || '').toLowerCase().includes(f))
      const st = String(w.lastStatus || '-').toUpperCase()
      const matchesStatus = !statusFilter || statusFilter === st
      return matchesName && matchesStatus
    })
  }, [workflowsWithPending, filter, statusFilter])

  function notify(message) {
    setActionMessage(message)
    window.setTimeout(() => setActionMessage(null), 7000)
  }

  async function handleAction(action, workflow) {
    setOpenMenuId(null)
    setActionMessage(null)
    if (action === 'history') return setModal({ type: 'history', workflow })
    if (action === 'dag') return setModal({ type: 'dag', workflow })
    if (action === 'edit') return setModal({ type: 'edit', workflow })

    setModal(null)
    try {
      if (action === 'run') {
        const result = await api.runWorkflow(workflow.workflowId, workflow.workflowName)
        const immediateLock = result.lock || {
          lockId: `local-${workflow.workflowId}-${Date.now()}`,
          workflowId: workflow.workflowId,
          workflowName: workflow.workflowName,
          runId: result.runId || 'pending',
          status: result.status || 'QUEUED',
          requestedBy: result.actor?.displayName || result.actor?.userName || 'current user',
          requestedAt: new Date().toISOString(),
          message: 'Queued. Waiting for dispatcher pickup.'
        }
        setGlobalLocks(prev => upsertLock(prev, immediateLock))
        setPendingRuns(prev => ({ ...prev, [workflow.workflowId]: { startedAt: Date.now(), runId: result.runId || 'pending', status: result.status || 'QUEUED' } }))
        notify(`Queued ${workflow.workflowName}. Run ID: ${result.runId || 'pending'}. Waiting for dispatcher pickup...`)
      }
      if (action === 'toggle-workflow') {
        await api.setWorkflowEnabled(workflow.workflowId, !workflow.workflowEnabled)
        notify(`${workflow.workflowEnabled ? 'Disabled' : 'Enabled'} ${workflow.workflowName}`)
        await load(true)
      }
      if (action === 'toggle-schedule') {
        await api.setScheduleEnabled(workflow.workflowId, !workflow.taskEnabled)
        notify(`${workflow.taskEnabled ? 'Disabled' : 'Enabled'} schedule for ${workflow.workflowName}`)
        await load(true)
      }
    } catch (err) {
      if (action === 'run') setPendingRuns(prev => { const next = { ...prev }; delete next[workflow.workflowId]; return next })
      notify(`Action failed for ${workflow.workflowName}: ${err.message}`)
    }
  }

  return (
    <section className="page monitor-page">
      <div className="page-hero monitor-hero">
        <div>
          <p className="breadcrumb">Pages / Monitor</p>
          <h1 className="page-heading">Workflow Monitor</h1>
          <p className="page-subtitle">Live operational control for KUMO workflow runs, schedules and dependencies.</p>
        </div>
        <button className="button refresh-button" onClick={() => load(true)}>↻ Refresh now</button>
      </div>

      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}
      {actionMessage && <div className="alert info">{actionMessage}</div>}

      <div className="monitor-toolbar">
        <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter workflows..." className="search-input" />
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="status-select">
          {statusOptions.map(option => <option key={option.value || 'all'} value={option.value}>{option.label}</option>)}
        </select>
      </div>

      <div className="monitor-system-strip">
        <div className={`system-engine-card ${statusKind(engine.status)}`}>
          <span className="system-pulse" />
          <div>
            <span>Engine</span>
            <strong>{engine.status || 'UNKNOWN'}</strong>
          </div>
        </div>
        <div className="system-counts">
          <SummaryItem tone="success" icon="✓" label="success" value={summary.success} />
          <SummaryItem tone="failed" icon="×" label="failed" value={summary.failed} />
          <SummaryItem tone="running" icon="▶" label="running/init" value={summary.running} />
          <SummaryItem tone="queued" icon="●" label="queued" value={summary.queued} />
          <span className="summary-total"><strong>{summary.total}</strong> total</span>
        </div>
        <span className="summary-updated">Updated {formatDateTime(payload?.generatedAt)}</span>
      </div>

      <div className="table-card monitor-table-card vision-card-flat">
        {loading ? <div className="empty-state">Loading monitor data...</div> : null}
        {!loading && filtered.length === 0 ? <div className="empty-state">No workflows match the current filters.</div> : null}
        {filtered.length > 0 && (
          <table className="workflow-table monitor-table">
            <thead><tr><th>Workflow</th><th>Manage</th><th>Status</th><th>Last Run</th><th>Duration</th><th>Schedule</th><th>Next Run</th></tr></thead>
            <tbody>{filtered.map(w => <WorkflowRow key={`${w.workflowId}-${w.lastRunId || 'none'}`} workflow={w} nowMs={nowMs} onManage={(workflow) => setModal({ type: 'actions', workflow })} pendingRun={pendingRuns[w.workflowId]} />)}</tbody>
          </table>
        )}
      </div>

      {modal?.type === 'actions' && <ActionsModal workflow={modal.workflow} pendingRun={pendingRuns[modal.workflow.workflowId]} onClose={() => setModal(null)} onAction={handleAction} />}
      {modal?.type === 'history' && <HistoryModal workflow={modal.workflow} onClose={() => setModal(null)} />}
      {modal?.type === 'dag' && <DagModal workflow={modal.workflow} onClose={() => setModal(null)} />}
      {modal?.type === 'edit' && <EditModal workflowId={modal.workflow.workflowId} onClose={() => setModal(null)} onSaved={() => load(true)} notify={notify} />}
    </section>
  )
}
