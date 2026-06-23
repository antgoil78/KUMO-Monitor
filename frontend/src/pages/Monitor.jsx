import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
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
  const pendingExpired = pendingRun && Date.now() - Number(pendingRun.startedAt || Date.now()) > 120000
  const backendBusy = isWorkflowBusy(workflow.lastStatus)
  const overlayPending = pendingRun && !pendingExpired && !backendBusy
  const view = overlayPending ? { ...workflow, lastStatus: pendingRun.status || 'INITIATING', lastRunId: pendingRun.runId || workflow.lastRunId, progress: { percent: null } } : workflow
  const busy = isWorkflowBusy(view.lastStatus)

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
      <td className="status-cell"><StatusBadge status={view.lastStatus} /></td>
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

  async function save() {
    setSaving(true)
    setError(null)
    try {
      await api.updateWorkflow(workflowId, detail)
      notify('Workflow saved')
      await onSaved()
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function clone() {
    setSaving(true)
    setError(null)
    try {
      await api.cloneWorkflow(workflowId)
      notify('Workflow cloned and disabled')
      await onSaved()
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    setSaving(true)
    setError(null)
    try {
      await api.deleteWorkflow(workflowId)
      notify('Workflow deleted')
      await onSaved()
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
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
  const view = pendingRun ? { ...workflow, lastStatus: 'INITIATING', lastRunId: pendingRun.runId || workflow.lastRunId } : workflow
  const wfEnabled = Boolean(view.workflowEnabled)
  const taskEnabled = Boolean(view.taskEnabled)
  const isDbt = String(view.workflowType || '').toUpperCase() === 'DBT'
  const busy = isWorkflowBusy(view.lastStatus)

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

      <div className="action-grid">
        <button className="action-tile primary" disabled={!wfEnabled || busy} onClick={() => choose('run')}>
          <span className="action-icon">▶</span>
          <strong>{busy ? 'Workflow active' : 'Run workflow'}</strong>
          <small>{busy ? 'Run is disabled while initiating, queued or running.' : 'Create a manual run request.'}</small>
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
  const [modal, setModal] = useState(null)

  async function load(force = false) {
    try {
      setError(null)
      const data = force ? await api.refreshMonitor() : await api.monitor()
      setPayload(data)
      setPendingRuns(prev => {
        const next = { ...prev }
        const seen = new Set((data.workflows || []).map(wf => wf.workflowId))
        const now = Date.now()

        for (const wf of data.workflows || []) {
          const pending = next[wf.workflowId]
          if (!pending) continue

          const status = String(wf.lastStatus || '').toUpperCase()
          const actualBusy = ['RUNNING', 'IN_PROGRESS', 'EXECUTING', 'QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED'].includes(status)
          const runVisible = pending.runId && pending.runId !== 'pending' && String(wf.lastRunId || '') === String(pending.runId)
          const expired = now - Number(pending.startedAt || now) > 120000

          if (actualBusy || runVisible || expired) {
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

  useEffect(() => { load(false) }, [])
  useEffect(() => {
    const intervalMs = payload?.refreshIntervalMs || 5000
    const id = setInterval(() => load(false), intervalMs)
    return () => clearInterval(id)
  }, [payload?.refreshIntervalMs])
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const workflows = payload?.workflows || []
  const workflowsWithPending = workflows.map(w => {
    const pending = pendingRuns[w.workflowId]
    if (!pending) return w
    const actualBusy = isWorkflowBusy(w.lastStatus)
    const expired = Date.now() - Number(pending.startedAt || Date.now()) > 120000
    if (actualBusy || expired) return w
    return { ...w, lastStatus: pending.status || 'INITIATING', lastRunId: pending.runId || w.lastRunId, progress: { percent: null } }
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
        setPendingRuns(prev => ({ ...prev, [workflow.workflowId]: { startedAt: Date.now(), runId: 'pending', status: 'INITIATING' } }))
        const result = await api.runWorkflow(workflow.workflowId)
        setPendingRuns(prev => ({ ...prev, [workflow.workflowId]: { startedAt: Date.now(), runId: result.runId || 'pending', status: 'QUEUED' } }))
        notify(`Initiated ${workflow.workflowName}. Run ID: ${result.runId || 'pending'}. Waiting for dispatcher pickup...`)
        await load(true)
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
