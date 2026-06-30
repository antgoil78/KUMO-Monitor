import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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

const activeRunStatuses = new Set([
  'INITIATING',
  'REQUESTED',
  'PENDING',
  'SCHEDULED',
  'QUEUED',
  'STARTING',
  'RUNNING',
  'IN_PROGRESS',
  'EXECUTING'
])

const runningRunStatuses = new Set(['RUNNING', 'IN_PROGRESS', 'EXECUTING', 'STARTING'])
const terminalRunStatuses = new Set(['SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK', 'FAILED', 'FAILURE', 'ERROR', 'CANCELLED', 'CANCELED', 'SKIPPED'])
const liveOverlayTtlMs = 30 * 60 * 1000
const terminalOverlayTtlMs = 30 * 1000

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

function rowKey(value) {
  return String(value || '')
}

function getAgeMs(value, fallbackMs = Date.now()) {
  if (!value) return Date.now() - fallbackMs
  if (typeof value === 'number') return Date.now() - value
  const parsed = new Date(value).getTime()
  return Number.isNaN(parsed) ? Date.now() - fallbackMs : Date.now() - parsed
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

function lockFromEvent(data, fallbackStatus = 'QUEUED') {
  const lock = data?.lock || {}
  const workflowId = rowKey(data?.workflowId || lock.workflowId)
  if (!workflowId) return null

  return {
    ...lock,
    lockId: lock.lockId || data?.lockId || '',
    workflowId,
    workflowName: data?.workflowName || lock.workflowName || workflowId,
    runId: data?.runId || lock.runId || '',
    status: normalizeStatus(data?.status || lock.status, fallbackStatus),
    requestedAt: data?.requestedAt || lock.requestedAt || new Date().toISOString(),
    requestedBy: data?.requestedBy || lock.requestedBy || data?.actor?.displayName || data?.actor?.userName || '',
    lastStartTime: data?.lastStartTime || lock.lastStartTime || null,
    lastEndTime: data?.lastEndTime || lock.lastEndTime || null,
    message: data?.message || lock.message || '',
    error: data?.error || lock.error || '',
    updatedAt: Date.now()
  }
}

function isFreshLock(lock) {
  const status = normalizeStatus(lock?.status, '-')
  const age = getAgeMs(lock?.updatedAt || lock?.requestedAt)
  if (terminalRunStatuses.has(status)) return age < terminalOverlayTtlMs
  return age < liveOverlayTtlMs
}

function upsertLock(locks, nextLock) {
  if (!nextLock?.workflowId) return locks || []

  const byWorkflowId = new Map((locks || []).map(item => [rowKey(item.workflowId), item]))
  const previous = byWorkflowId.get(rowKey(nextLock.workflowId)) || {}
  const previousStatus = normalizeStatus(previous.status, '')
  const nextStatus = normalizeStatus(nextLock.status, '')
  const previousRunId = rowKey(previous.runId)
  const nextRunId = rowKey(nextLock.runId || previousRunId)
  const sameRun = previousRunId && nextRunId && previousRunId === nextRunId
  const isStatusDowngrade = sameRun && previousStatus && nextStatus && (statusRank[previousStatus] || 0) > (statusRank[nextStatus] || 0)

  const merged = isStatusDowngrade
    ? { ...nextLock, ...previous, status: previousStatus, runId: previousRunId, updatedAt: previous.updatedAt || Date.now() }
    : { ...previous, ...nextLock, status: nextStatus || previousStatus || 'QUEUED', runId: nextRunId, updatedAt: nextLock.updatedAt || Date.now() }

  byWorkflowId.set(rowKey(nextLock.workflowId), merged)
  return Array.from(byWorkflowId.values()).filter(isFreshLock)
}

function locksFromPayload(payload) {
  const directLocks = payload?.runLocks || payload?.workflowRunLocks || payload?.locks || []
  const workflowLocks = (payload?.workflows || []).map(workflow => workflow.runLock).filter(Boolean)
  return [...directLocks, ...workflowLocks]
    .filter(lock => lock?.workflowId)
    .map(lock => ({ ...lock, status: normalizeStatus(lock.status, 'QUEUED'), updatedAt: lock.updatedAt || Date.now() }))
    .filter(isFreshLock)
}

function mergeLocks(previousLocks, nextLocks) {
  return (nextLocks || []).reduce((out, lock) => upsertLock(out, lock), previousLocks || []).filter(isFreshLock)
}

function reconcileLocksWithWorkflows(locks, workflows) {
  if (!locks?.length) return []

  const byWorkflowId = new Map((workflows || []).map(workflow => [rowKey(workflow.workflowId), workflow]))

  return locks.reduce((out, lock) => {
    const workflow = byWorkflowId.get(rowKey(lock.workflowId))
    if (!workflow) {
      if (isFreshLock(lock)) out.push(lock)
      return out
    }

    if (workflow.runLock) {
      const merged = upsertLock([lock], workflow.runLock)[0]
      if (merged && isFreshLock(merged)) out.push(merged)
      return out
    }

    const sameRun = lock.runId && rowKey(workflow.lastRunId) === rowKey(lock.runId)
    const workflowStatus = normalizeStatus(workflow.lastStatus, '-')
    const lockStatus = normalizeStatus(lock.status, '-')

    if (sameRun && terminalRunStatuses.has(workflowStatus) && terminalRunStatuses.has(lockStatus)) {
      return out
    }

    if (isFreshLock(lock)) out.push(lock)
    return out
  }, [])
}

function overlayWorkflow(workflow, locks) {
  const lock = (locks || []).find(item => rowKey(item.workflowId) === rowKey(workflow.workflowId))
  if (!lock) return workflow

  const status = normalizeStatus(lock.status, 'QUEUED')
  const terminal = terminalRunStatuses.has(status)
  const active = activeRunStatuses.has(status)

  return {
    ...workflow,
    lastStatus: status,
    lastRunId: lock.runId || workflow.lastRunId,
    lastRequestedAt: lock.requestedAt || workflow.lastRequestedAt,
    lastRequestedBy: lock.requestedBy || workflow.lastRequestedBy,
    lastStartTime: lock.lastStartTime || workflow.lastStartTime,
    lastEndTime: terminal ? (lock.lastEndTime || workflow.lastEndTime || new Date().toISOString()) : workflow.lastEndTime,
    progress: active ? (workflow.progress || { percent: null }) : workflow.progress,
    runLocked: active,
    runLock: active || terminal ? { ...(workflow.runLock || {}), ...lock } : workflow.runLock
  }
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
  const status = normalizeStatus(workflow.lastStatus, '-')
  if (!activeRunStatuses.has(status) && !workflow.progress) return null

  return (
    <div className="inline-progress">
      <ProgressBar progress={workflow.progress} status={workflow.lastStatus} />
    </div>
  )
}

function useOutsideClick(ref, onClose) {
  useEffect(() => {
    function handleMouseDown(event) {
      if (!ref.current || ref.current.contains(event.target)) return
      onClose()
    }

    function handleEscape(event) {
      if (event.key === 'Escape') onClose()
    }

    document.addEventListener('mousedown', handleMouseDown)
    document.addEventListener('keydown', handleEscape)
    return () => {
      document.removeEventListener('mousedown', handleMouseDown)
      document.removeEventListener('keydown', handleEscape)
    }
  }, [ref, onClose])
}

function RowActions({ workflow, isOpen, onOpen, onClose, onAction, disabledRun }) {
  const ref = useRef(null)
  useOutsideClick(ref, onClose)

  const workflowEnabled = Boolean(workflow.workflowEnabled)
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
        ...
      </button>

      {isOpen && (
        <div className="row-menu-popover">
          <button disabled={disabledRun || !workflowEnabled} onClick={() => click('run')}>Run workflow</button>
          {isDbt && <button onClick={() => click('dag')}>Show DAG run</button>}
          <button onClick={() => click('history')}>History</button>
          <button onClick={() => click('edit')}>Edit</button>
          <button onClick={() => click('toggle-workflow')}>{workflowEnabled ? 'Disable workflow' : 'Enable workflow'}</button>
          <button onClick={() => click('toggle-schedule')}>{taskEnabled ? 'Disable schedule' : 'Enable schedule'}</button>
          {workflow.lastRunId && <div className="row-menu-meta">Run ID: <code>{workflow.lastRunId}</code></div>}
        </div>
      )}
    </div>
  )
}

function WorkflowRow({ workflow, nowMs, onAction, openMenuId, setOpenMenuId, onManage }) {
  const disabled = !workflow.workflowEnabled
  const depth = Number(workflow.indent || 0)
  const type = String(workflow.workflowType || 'DBT').toUpperCase()
  const isRoot = depth === 0
  const busy = isWorkflowBusy(workflow.lastStatus) || Boolean(workflow.runLocked)
  const menuOpen = openMenuId === workflow.workflowId

  return (
    <tr className={`${disabled ? 'disabled-row' : ''} ${isRoot ? 'root-row' : 'child-row'}`}>
      <td>
        <div className="workflow-name-cell" style={{ paddingLeft: `${depth * 18}px` }}>
          {depth > 0 && <span className="tree-branch" />}
          <div>
            <strong>{workflow.workflowName}</strong>
            <div className="workflow-subtitle">{workflow.workflowGroup || 'Ungrouped'} · {type}</div>
            {workflow.runLocked && <div className="workflow-subtitle">Locked by {workflow.runLock?.requestedBy || 'another user'}</div>}
          </div>
        </div>
      </td>
      <td>
        <StatusBadge status={workflow.lastStatus} />
        <RunningProgress workflow={workflow} />
      </td>
      <td>
        <span>{formatDateTime(workflow.lastStartTime)}</span>
        {workflow.lastRunId && <code className="run-id-inline">{workflow.lastRunId}</code>}
      </td>
      <td>{elapsedDuration(workflow.lastStartTime, workflow.lastEndTime, workflow.lastStatus, nowMs)}</td>
      <td>{workflow.taskEnabled ? <><code>{workflow.scheduleCron || '-'}</code><small>{workflow.scheduleTimezone || 'UTC'}</small></> : '-'}</td>
      <td>{workflow.taskEnabled ? formatDateTime(workflow.nextRunTime) : '-'}</td>
      <td className="actions-cell">
        <button className="button muted small" onClick={() => onManage(workflow)}>Manage</button>
        <RowActions
          workflow={workflow}
          isOpen={menuOpen}
          onOpen={() => setOpenMenuId(workflow.workflowId)}
          onClose={() => setOpenMenuId(null)}
          onAction={onAction}
          disabledRun={busy}
        />
      </td>
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
    <div className="modal-backdrop">
      <div className={`modal-card ${wide ? 'wide' : ''}`}>
        <div className="modal-header">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="modal-close" onClick={onClose}>x</button>
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
    <div className="form-field multi-select-field">
      <label>{label}</label>
      <div className="multi-select-list">
        {options.length === 0 ? (
          <span className="soft-empty">No workflows available</span>
        ) : options.map(option => (
          <label key={option.workflowId}>
            <input type="checkbox" checked={selected.has(option.workflowId)} onChange={() => toggle(option.workflowId)} />
            {option.label}
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
          setError(`Workflow details are still loading. Check backend logs for /api/workflows/${workflowId} or try again.`)
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
    Promise.resolve(onSaved()).catch(err => notify(`Refresh failed after update: ${err.message}`))
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

  async function retry() {
    setError(null)
    setDetail(null)
    try {
      const data = await api.workflowDetail(workflowId, { timeoutMs: 60000 })
      setDetail(data)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <Modal title="Edit workflow" subtitle={detail?.workflowName || workflowId} onClose={onClose} wide>
      {!detail && !error && <div className="empty-state">Loading workflow...</div>}
      {error && <div className="alert error">{error}</div>}
      {error && !detail && (
        <div className="modal-actions">
          <button className="button" onClick={retry}>Retry loading workflow</button>
          <button className="button muted" onClick={onClose}>Close</button>
        </div>
      )}

      {detail && (
        <div className="edit-form">
          <div className="form-grid two">
            <div className="form-field">
              <label>Name</label>
              <input value={detail.workflowName || ''} onChange={event => patch('workflowName', event.target.value)} />
            </div>
            <div className="form-field">
              <label>Group</label>
              <input value={detail.workflowGroup || ''} onChange={event => patch('workflowGroup', event.target.value)} />
            </div>
          </div>

          <div className="form-grid two">
            <div className="form-field">
              <label>Type</label>
              <select value={detail.workflowType || 'DBT'} onChange={event => patch('workflowType', event.target.value)}>
                <option value="DBT">DBT</option>
                <option value="SQL">SQL</option>
              </select>
            </div>
            <div className="toggle-row vertical">
              <label><input type="checkbox" checked={Boolean(detail.workflowEnabled)} onChange={event => patch('workflowEnabled', event.target.checked)} /> Workflow enabled</label>
              <label><input type="checkbox" checked={Boolean(detail.taskEnabled)} onChange={event => patch('taskEnabled', event.target.checked)} /> Schedule enabled</label>
            </div>
          </div>

          <div className="form-field">
            <label>Description</label>
            <textarea rows="2" value={detail.description || ''} onChange={event => patch('description', event.target.value)} />
          </div>

          {String(detail.workflowType || '').toUpperCase() === 'DBT' ? (
            <>
              <div className="form-field">
                <label>DBT Command</label>
                <textarea rows="3" value={detail.dbtCommand || ''} onChange={event => patch('dbtCommand', event.target.value)} />
              </div>
              <div className="form-grid two">
                <div className="form-field">
                  <label>DBT Project FQN</label>
                  <input value={detail.dbtProjectFqn || ''} onChange={event => patch('dbtProjectFqn', event.target.value)} />
                </div>
                <div className="form-field">
                  <label>DBT Target</label>
                  <input value={detail.dbtTarget || ''} onChange={event => patch('dbtTarget', event.target.value)} />
                </div>
              </div>
            </>
          ) : (
            <div className="form-field">
              <label>SQL Command</label>
              <textarea rows="5" value={detail.sqlCommand || ''} onChange={event => patch('sqlCommand', event.target.value)} />
            </div>
          )}

          <div className="form-grid two">
            <div className="form-field">
              <label>Cron</label>
              <input value={detail.scheduleCron || ''} onChange={event => patch('scheduleCron', event.target.value)} />
            </div>
            <div className="form-field">
              <label>Timezone</label>
              <input value={detail.scheduleTimezone || 'UTC'} onChange={event => patch('scheduleTimezone', event.target.value)} />
            </div>
          </div>

          <div className="form-grid two">
            <MultiSelect label="On Success" options={detail.workflowOptions || []} value={detail.onSuccess || []} onChange={value => patch('onSuccess', value)} />
            <MultiSelect label="On Fail" options={detail.workflowOptions || []} value={detail.onFail || []} onChange={value => patch('onFail', value)} />
          </div>

          <details className="advanced-section">
            <summary>Notifications</summary>
            <div className="form-grid two">
              <div className="toggle-row vertical">
                <label><input type="checkbox" checked={Boolean(detail.notifications?.onFailEmail)} onChange={event => patchNotif('onFailEmail', event.target.checked)} /> Email on failure</label>
                <label><input type="checkbox" checked={Boolean(detail.notifications?.onSuccessEmail)} onChange={event => patchNotif('onSuccessEmail', event.target.checked)} /> Email on success</label>
              </div>
              <div className="form-field">
                <label>Email integration</label>
                <input value={detail.notifications?.emailIntegration || ''} onChange={event => patchNotif('emailIntegration', event.target.value)} />
              </div>
              <div className="form-field">
                <label>Fail group</label>
                <input list="email-groups" value={detail.notifications?.failGroup || ''} onChange={event => patchNotif('failGroup', event.target.value)} />
              </div>
              <div className="form-field">
                <label>Success group</label>
                <input list="email-groups" value={detail.notifications?.successGroup || ''} onChange={event => patchNotif('successGroup', event.target.value)} />
              </div>
              <div className="form-field">
                <label>Environment</label>
                <input value={detail.notifications?.environment || ''} onChange={event => patchNotif('environment', event.target.value)} />
              </div>
            </div>
            <datalist id="email-groups">{(detail.emailGroups || []).map(group => <option key={group} value={group} />)}</datalist>
          </details>

          {confirmDelete && <div className="alert warning">Delete this workflow and related queue/history/task rows? This cannot be undone.</div>}

          <div className="modal-actions">
            <button className="button primary" disabled={saving} onClick={save}>Save</button>
            <button className="button" disabled={saving} onClick={clone}>Clone</button>
            {!confirmDelete ? (
              <button className="button danger" disabled={saving} onClick={() => setConfirmDelete(true)}>Delete</button>
            ) : (
              <button className="button danger" disabled={saving} onClick={remove}>Confirm delete</button>
            )}
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
    api.workflowHistory(workflow.workflowId)
      .then(data => { if (!cancelled) setRows(data.rows || []) })
      .catch(err => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [workflow.workflowId])

  return (
    <Modal title="Workflow history" subtitle={workflow.workflowName} onClose={onClose} wide>
      {error && <div className="alert error">{error}</div>}
      {!rows && !error && <div className="empty-state">Loading history...</div>}
      {rows && (
        <div className="modal-table-wrap">
          <table className="workflow-table compact">
            <thead>
              <tr><th>Run ID</th><th>Status</th><th>Requested</th><th>Start</th><th>End</th><th>Error</th></tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={row.RUN_ID || row.runId}>
                  <td><code>{row.RUN_ID || row.runId}</code></td>
                  <td><StatusBadge status={row.STATUS || row.status} /></td>
                  <td>{formatDateTime(row.REQUESTED_AT || row.requestedAt)}</td>
                  <td>{formatDateTime(row.START_TIME || row.startTime)}</td>
                  <td>{formatDateTime(row.END_TIME || row.endTime)}</td>
                  <td>{String(row.ERROR_MESSAGE || row.errorMessage || '').slice(0, 120)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  )
}

function DagModal({ workflow, onClose }) {
  const [dag, setDag] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    api.workflowDag(workflow.workflowId)
      .then(data => { if (!cancelled) setDag(data) })
      .catch(err => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [workflow.workflowId])

  const nodes = dag?.nodes || []
  const done = nodes.filter(node => ['DONE', 'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK'].includes(normalizeStatus(node.status, '-'))).length
  const failed = nodes.filter(node => ['ERROR', 'FAILED', 'FAILURE'].includes(normalizeStatus(node.status, '-'))).length
  const percent = nodes.length ? Math.round((done / nodes.length) * 100) : 0

  return (
    <Modal title="DAG run" subtitle={workflow.workflowName} onClose={onClose} wide>
      {error && <div className="alert error">{error}</div>}
      {!dag && !error && <div className="empty-state">Loading DAG...</div>}
      {dag && (
        <>
          <div className="dag-summary">
            <StatusBadge status={dag.run?.STATUS || dag.run?.status || '-'} />
            <span>Run ID <code>{dag.run?.RUN_ID || dag.run?.runId || '-'}</code></span>
            <span>{done}/{nodes.length} completed</span>
            {failed > 0 && <span className="failed-text">{failed} failed</span>}
          </div>
          <div className="dag-progress">
            <ProgressBar progress={{ percent, total: nodes.length, done, failed }} status={dag.run?.STATUS || dag.run?.status} />
          </div>
          <div className="dag-node-grid">
            {nodes.length ? nodes.map(node => (
              <div className={`dag-node ${statusKind(node.status)}`} key={node.id} title={node.id}>{node.label || node.id}</div>
            )) : <div className="soft-empty">No execution progress data for this run.</div>}
          </div>
          {dag.errors?.length > 0 && (
            <div className="modal-table-wrap">
              <table className="workflow-table compact">
                <thead><tr><th>Time</th><th>Model</th><th>Error</th></tr></thead>
                <tbody>{dag.errors.map((err, index) => (
                  <tr key={`${err.ORIGIN || err.origin}-${index}`}>
                    <td>{formatDateTime(err.LOG_DTTM || err.logDttm)}</td>
                    <td>{err.ORIGIN || err.origin}</td>
                    <td>{String(err.MESSAGE || err.message || '').slice(0, 240)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Modal>
  )
}

function ActionsModal({ workflow, onClose, onAction }) {
  const workflowEnabled = Boolean(workflow.workflowEnabled)
  const taskEnabled = Boolean(workflow.taskEnabled)
  const isDbt = String(workflow.workflowType || '').toUpperCase() === 'DBT'
  const busy = isWorkflowBusy(workflow.lastStatus) || Boolean(workflow.runLocked)

  async function choose(action) {
    await onAction(action, workflow)
  }

  return (
    <Modal title="Workflow actions" subtitle={workflow.workflowName} onClose={onClose}>
      <div className="action-modal-head">
        <div>
          <span className="modal-eyebrow">Current status</span>
          <StatusBadge status={workflow.lastStatus} />
        </div>
        {workflow.lastRunId && <code>{workflow.lastRunId}</code>}
      </div>

      {workflow.runLocked && (
        <div className="alert info compact">
          This workflow is locked for a pending run. Requested by {workflow.runLock?.requestedBy || 'unknown user'}.
        </div>
      )}

      <div className="action-grid">
        <button className="action-tile primary" disabled={!workflowEnabled || busy} onClick={() => choose('run')}>
          <span className="action-icon">Run</span>
          <strong>{busy ? 'Workflow active' : 'Run workflow'}</strong>
          <small>{busy ? 'Run is disabled while initiating, queued or running.' : 'Create a manual run request.'}</small>
        </button>

        {isDbt && (
          <button className="action-tile" onClick={() => choose('dag')}>
            <span className="action-icon">DAG</span>
            <strong>Show DAG run</strong>
            <small>Open latest DBT execution progress.</small>
          </button>
        )}

        <button className="action-tile" onClick={() => choose('history')}>
          <span className="action-icon">Hist</span>
          <strong>History</strong>
          <small>View recent workflow executions.</small>
        </button>

        <button className="action-tile" onClick={() => choose('edit')}>
          <span className="action-icon">Edit</span>
          <strong>Edit workflow</strong>
          <small>Change metadata, schedule, dependencies and notifications.</small>
        </button>

        <button className="action-tile" onClick={() => choose('toggle-workflow')}>
          <span className="action-icon">WF</span>
          <strong>{workflowEnabled ? 'Disable workflow' : 'Enable workflow'}</strong>
          <small>{workflowEnabled ? 'Also prevents future manual runs.' : 'Allow workflow runs again.'}</small>
        </button>

        <button className="action-tile" disabled={!workflowEnabled && !taskEnabled} onClick={() => choose('toggle-schedule')}>
          <span className="action-icon">Sch</span>
          <strong>{taskEnabled ? 'Disable schedule' : 'Enable schedule'}</strong>
          <small>{taskEnabled ? 'Keep workflow but stop timed triggers.' : 'Resume scheduled execution.'}</small>
        </button>
      </div>
    </Modal>
  )
}

export default function Monitor() {
  const [payload, setPayload] = useState(null)
  const [locks, setLocks] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [actionMessage, setActionMessage] = useState(null)
  const [openMenuId, setOpenMenuId] = useState(null)
  const [modal, setModal] = useState(null)
  const [realtimeFallback, setRealtimeFallback] = useState(false)

  function notify(message) {
    setActionMessage(message)
    window.setTimeout(() => setActionMessage(null), 7000)
  }

  const applyMonitorPayload = useCallback((data) => {
    setPayload(data)
    setLocks(previous => reconcileLocksWithWorkflows(mergeLocks(previous, locksFromPayload(data)), data?.workflows || []))
    setLoading(false)
  }, [])

  const load = useCallback(async (force = false) => {
    try {
      setError(null)
      const data = force ? await api.refreshMonitor() : await api.monitor()
      applyMonitorPayload(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [applyMonitorPayload])

  useEffect(() => {
    load(false)
  }, [load])

  useEffect(() => {
    const source = createKumoEventSource((event) => {
      const type = event?.type
      const data = event?.data || {}

      if (type === 'connected') {
        setRealtimeFallback(false)
        return
      }

      if (type === 'monitor_update') {
        applyMonitorPayload(data)
        setRealtimeFallback(false)
        return
      }

      if (['workflow_run_requested', 'workflow_run_queued', 'workflow_run_status', 'workflow_run_failed'].includes(type)) {
        const fallbackStatus = type === 'workflow_run_failed' ? 'FAILED' : type === 'workflow_run_requested' ? 'INITIATING' : 'QUEUED'
        const lock = lockFromEvent(data, fallbackStatus)
        if (!lock) return

        const patchedLock = type === 'workflow_run_failed' ? { ...lock, status: 'FAILED' } : lock
        setLocks(previous => upsertLock(previous, patchedLock))
        setPayload(previous => {
          if (!previous?.workflows?.length) return previous
          const workflows = previous.workflows.map(workflow => {
            if (rowKey(workflow.workflowId) !== rowKey(patchedLock.workflowId)) return workflow
            return overlayWorkflow(workflow, [patchedLock])
          })
          return { ...previous, workflows, summary: buildSummary(workflows), generatedAt: previous.generatedAt || new Date().toISOString() }
        })

        if (type === 'workflow_run_failed') {
          notify(`Run request failed for ${patchedLock.workflowName}: ${patchedLock.error || 'Unknown error'}`)
        }
      }
    }, () => {
      setRealtimeFallback(true)
    })

    if (!source) {
      setRealtimeFallback(true)
    }

    return () => source?.close()
  }, [applyMonitorPayload])

  useEffect(() => {
    if (!realtimeFallback) return undefined

    const intervalMs = Math.max(Number(payload?.refreshIntervalMs || 30000), 30000)
    const id = setInterval(() => load(false), intervalMs)
    return () => clearInterval(id)
  }, [payload?.refreshIntervalMs, realtimeFallback, load])

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const workflows = payload?.workflows || []
  const workflowsWithLocks = useMemo(() => workflows.map(workflow => overlayWorkflow(workflow, locks)), [workflows, locks])

  const summary = useMemo(() => buildSummary(workflowsWithLocks), [workflowsWithLocks])
  const engine = payload?.engine || { status: 'UNKNOWN' }

  const filtered = useMemo(() => {
    const value = filter.trim().toLowerCase()
    return workflowsWithLocks.filter(workflow => {
      const matchesName = !value || [workflow.workflowName, workflow.workflowGroup, workflow.workflowType, workflow.lastRunId]
        .some(item => String(item || '').toLowerCase().includes(value))
      const status = normalizeStatus(workflow.lastStatus, '-')
      const matchesStatus = !statusFilter || statusFilter === status
      return matchesName && matchesStatus
    })
  }, [workflowsWithLocks, filter, statusFilter])

  async function handleAction(action, workflow) {
    setOpenMenuId(null)
    setActionMessage(null)

    if (action === 'history') return setModal({ type: 'history', workflow })
    if (action === 'dag') return setModal({ type: 'dag', workflow })
    if (action === 'edit') return setModal({ type: 'edit', workflow })

    setModal(null)

    try {
      if (action === 'run') {
        const optimisticLock = {
          workflowId: workflow.workflowId,
          workflowName: workflow.workflowName,
          status: 'INITIATING',
          runId: '',
          requestedAt: new Date().toISOString(),
          requestedBy: '',
          updatedAt: Date.now()
        }

        setLocks(previous => upsertLock(previous, optimisticLock))

        const result = await api.runWorkflow(workflow.workflowId, workflow.workflowName)
        const resultLock = lockFromEvent({
          ...result,
          workflowId: workflow.workflowId,
          workflowName: workflow.workflowName,
          status: result.status || 'QUEUED'
        }, 'QUEUED')

        if (resultLock) {
          setLocks(previous => upsertLock(previous, resultLock))
        }

        notify(`Initiated ${workflow.workflowName}. Run ID: ${result.runId || '-'}`)
        return
      }

      if (action === 'toggle-workflow') {
        await api.setWorkflowEnabled(workflow.workflowId, !workflow.workflowEnabled)
        notify(`${workflow.workflowEnabled ? 'Disabled' : 'Enabled'} ${workflow.workflowName}`)
        await load(true)
        return
      }

      if (action === 'toggle-schedule') {
        await api.setScheduleEnabled(workflow.workflowId, !workflow.taskEnabled)
        notify(`${workflow.taskEnabled ? 'Disabled' : 'Enabled'} schedule for ${workflow.workflowName}`)
        await load(true)
      }
    } catch (err) {
      if (action === 'run') {
        setLocks(previous => previous.filter(lock => rowKey(lock.workflowId) !== rowKey(workflow.workflowId)))
      }
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
        <button className="button refresh-button" onClick={() => load(true)}>Refresh now</button>
      </div>

      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}
      {actionMessage && <div className="alert info">{actionMessage}</div>}
      {realtimeFallback && <div className="alert warning">Realtime stream is unavailable. Browser fallback polling is active.</div>}

      <div className="monitor-toolbar">
        <input value={filter} onChange={event => setFilter(event.target.value)} placeholder="Filter workflows..." className="search-input" />
        <select value={statusFilter} onChange={event => setStatusFilter(event.target.value)} className="status-select">
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
          <SummaryItem tone="success" icon="OK" label="success" value={summary.success} />
          <SummaryItem tone="failed" icon="ERR" label="failed" value={summary.failed} />
          <SummaryItem tone="running" icon="RUN" label="running/init" value={summary.running} />
          <SummaryItem tone="queued" icon="QUE" label="queued" value={summary.queued} />
          <span className="summary-total"><strong>{summary.total}</strong> total</span>
        </div>
        <span className="summary-updated">Updated {formatDateTime(payload?.generatedAt)}</span>
      </div>

      <div className="table-card monitor-table-card vision-card-flat">
        {loading && <div className="empty-state">Loading monitor data...</div>}
        {!loading && filtered.length === 0 && <div className="empty-state">No workflows match the current filters.</div>}
        {filtered.length > 0 && (
          <table className="workflow-table monitor-table">
            <thead>
              <tr><th>Workflow</th><th>Status</th><th>Last Run</th><th>Duration</th><th>Schedule</th><th>Next Run</th><th /></tr>
            </thead>
            <tbody>
              {filtered.map(workflow => (
                <WorkflowRow
                  key={`${workflow.workflowId}-${workflow.lastRunId || 'none'}`}
                  workflow={workflow}
                  nowMs={nowMs}
                  onAction={handleAction}
                  openMenuId={openMenuId}
                  setOpenMenuId={setOpenMenuId}
                  onManage={item => setModal({ type: 'actions', workflow: item })}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {modal?.type === 'actions' && <ActionsModal workflow={modal.workflow} onClose={() => setModal(null)} onAction={handleAction} />}
      {modal?.type === 'history' && <HistoryModal workflow={modal.workflow} onClose={() => setModal(null)} />}
      {modal?.type === 'dag' && <DagModal workflow={modal.workflow} onClose={() => setModal(null)} />}
      {modal?.type === 'edit' && <EditModal workflowId={modal.workflow.workflowId} onClose={() => setModal(null)} onSaved={() => load(true)} notify={notify} />}
    </section>
  )
}
