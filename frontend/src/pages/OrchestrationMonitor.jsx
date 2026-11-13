import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api.js'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatDateTime, lastRunDay } from '../utils/time.js'
import EditorDialog from '../components/EditorDialog.jsx'
import HierarchyToggle from '../components/HierarchyToggle.jsx'
import ActionIcon from '../components/ActionIcon.jsx'
import OrchestrationTimeline from '../components/OrchestrationTimeline.jsx'
import useOrchestrationMonitor, { activeWorkflowStatuses, applySubmission } from '../utils/useOrchestrationMonitor.js'
import { RunWorkflowDialog } from './WorkflowExecution.jsx'
import './OrchestrationMonitor.css'

const statuses = ['RUNNING', 'CLAIMED', 'QUEUED', 'FAILED', 'SUCCESS', 'WARNING', 'SKIPPED', 'CANCELLED', 'NOT RUN']
const summaryStatuses = [
  ['SUCCESS', 'success', '✓', 'success'], ['FAILED', 'failed', '×', 'failed'],
  ['RUNNING', 'running', '▶', 'running/init'], ['QUEUED', 'queued', '●', 'queued'],
  ['NOT RUN', 'muted', '—', 'not run'], ['WARNING', 'warning', '!', 'warning'],
  ['SKIPPED', 'skipped', '↷', 'skipped'], ['CANCELLED', 'muted', '—', 'cancelled']
]
const timeFormatter = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit', second: '2-digit' })

function asDate(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function durationLabel(run, now) {
  const start = asDate(run.start)
  const end = asDate(run.end) || (['RUNNING', 'CLAIMED'].includes(run.status) ? new Date(now) : null)
  if (!start || !end) return '—'
  const seconds = Math.max(0, Math.floor((end - start) / 1000))
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`
}

function matches(workflow, query) {
  return [workflow.WORKFLOW_NAME, workflow.WORKFLOW_GROUP, ...workflow.jobs.map(job => job.JOB_NAME)]
    .some(value => String(value || '').toLowerCase().includes(query))
}

function RunCells({ run, lastRun, now }) {
  const timing = lastRun || run
  const start = asDate(timing.start)
  const day = lastRunDay(timing.start, now)
  return <><td><span className="om-live-status">{['INITIATING', 'CLAIMED', 'QUEUED', 'RUNNING'].includes(run.status) && <span className="om-spinner" role="img" aria-label={run.status === 'RUNNING' ? 'Execution in progress' : 'Preparing execution'} />}<StatusBadge status={run.status === 'CLAIMED' ? 'INITIATING' : run.status} /></span></td><td className="om-time" title={timing.start || ''}>{start ? <>{timeFormatter.format(start)}<small className={day.tone ? `om-day-${day.tone}` : ''}>{day.label}</small></> : '—'}</td><td className="om-time">{durationLabel(timing, now)}</td></>
}

function RunActions({ name, displayName, disabled, reason, onRun, onHistory, onRunFrom, onDag, canDag, onLog, canLog, onState, enabled, busy, onSchedule, scheduleEnabled, onEditSchedule }) {
  const [menu, setMenu] = useState(null)
  const trigger = useRef(null)
  const options = useRef(null)
  useEffect(() => {
    if (!menu) return undefined
    function dismiss(event) {
      if (!options.current?.contains(event.target) && !trigger.current?.contains(event.target)) setMenu(null)
    }
    function keydown(event) {
      if (event.key === 'Escape') { setMenu(null); trigger.current?.focus() }
    }
    const close = event => {
      if (event.type === 'scroll' && options.current?.contains(event.target)) return
      setMenu(null)
    }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', keydown)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', close, true)
    options.current?.querySelector('button:not(:disabled)')?.focus()
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', keydown)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [menu])
  function toggle() {
    if (menu) { setMenu(null); return }
    const box = trigger.current.getBoundingClientRect()
    const width = Math.min(252, window.innerWidth - 16)
    const height = onSchedule ? 376 : onDag ? 344 : 302
    const above = box.top - 8
    const below = window.innerHeight - box.bottom - 8
    const upwards = below < height && above > below
    setMenu({ width, left: Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8)),
      maxHeight: Math.max(120, upwards ? above - 4 : below - 4),
      ...(upwards ? { bottom: window.innerHeight - box.top + 4 } : { top: box.bottom + 4 }) })
  }
  function act(callback) { setMenu(null); trigger.current?.focus(); callback() }
  return <div className="om-row-actions">
    {onDag && <button className="om-shortcut" type="button" title={canDag ? 'Latest DAG Run' : 'No started job execution recorded'} aria-label={`Latest DAG Run for ${name}`} disabled={!canDag} onClick={onDag}><ActionIcon name="dag" /></button>}
    <button className="om-shortcut" type="button" title={canLog ? 'View execution log' : 'No execution recorded'} aria-label={`Log for ${name}`} disabled={!canLog} onClick={onLog}><ActionIcon name="log" /></button>
    <button className="om-play" type="button" aria-label={`Run ${name}`} title={disabled ? reason : `Run ${name}`} disabled={disabled} onClick={onRun}><ActionIcon name="play" /></button>
    <button className="om-more" type="button" ref={trigger} aria-label={`Actions for ${name}`} aria-expanded={Boolean(menu)} title="Actions" onClick={toggle}><span aria-hidden="true">⋯</span></button>
    {menu && createPortal(<div className="om-action-options" role="group" aria-label={`Actions for ${name}`} ref={options} style={menu}>
      <div className="om-action-heading"><span>{onRunFrom ? 'Job actions' : 'Workflow actions'}</span><strong title={displayName}>{displayName}</strong></div>
      <div className="om-action-section">
        <button type="button" className="om-action-run" disabled={disabled} onClick={() => act(onRun)}><ActionIcon name="play" /><span>Run{onRunFrom ? ' only this job' : ' workflow'}</span></button>
        {onRunFrom && <button type="button" className="om-action-run" disabled={disabled} onClick={() => act(onRunFrom)}><ActionIcon name="from" /><span>Run from this job</span></button>}
      </div>
      <div className="om-action-section">
        {onDag && <button type="button" disabled={!canDag} onClick={() => act(onDag)}><ActionIcon name="dag" /><span>Latest DAG Run</span></button>}
        <button type="button" disabled={!canLog} onClick={() => act(onLog)}><ActionIcon name="log" /><span>View log</span></button>
        <button type="button" onClick={() => act(onHistory)}><ActionIcon name="history" /><span>Execution history</span></button>
      </div>
      <div className="om-action-section">
        <button type="button" disabled={busy || !onState} onClick={() => act(onState)}><ActionIcon name={enabled ? 'pause' : 'power'} /><span>{enabled ? 'Disable' : 'Enable'} {onRunFrom ? 'job' : 'workflow'}</span></button>
        {onSchedule && <><button type="button" disabled={busy} onClick={() => act(onSchedule)}><ActionIcon name={scheduleEnabled ? 'calendarOff' : 'calendar'} /><span>{scheduleEnabled ? 'Disable schedule' : 'Enable schedule'}</span></button><button type="button" disabled={busy} onClick={() => act(onEditSchedule)}><ActionIcon name="edit" /><span>Edit schedule…</span></button></>}
      </div>
    </div>, document.body)}
  </div>
}

function JobProgress({ job }) {
  const progress = job.progress
  if (!progress?.total) return <span className="om-muted">{job.run.status === 'RUNNING' && job.JOB_TYPE === 'DBT' ? 'Waiting for dbt progress…' : jobProgress(job)}</span>
  return <div className={`om-progress ${progress.models?.failed || progress.tests?.failed ? 'has-failure' : ''}`}>
    {['models', 'tests'].map(kind => {
      const part = progress[kind]
      if (!part?.total) return null
      const label = `${part.completed} / ${part.total} ${kind}${part.running ? ` · ${part.running} running` : ''}${part.failed ? ` · ${part.failed} failed` : ''}${part.skipped ? ` · ${part.skipped} skipped` : ''}${part.warning ? ` · ${part.warning} warnings` : ''}`
      return <span key={kind} title={label}>{label}</span>
    })}
    <div className="om-track" role="progressbar" aria-label={`DBT progress for ${job.JOB_NAME}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.percent}><i style={{ width: `${progress.percent}%` }} /></div>
  </div>
}

function dependencyLabel(job) {
  if (job.ALLOW === false) return 'Blocked by ALLOW'
  const allowed = ['SUCCESS', 'WARNING', 'ERROR'].filter(status => job[status] !== false).map(status => status.toLowerCase())
  return `${allowed.length ? allowed.join(' / ') : 'No parent result allowed'}${job.hasSqlCondition ? ' · SQL condition' : ''}`
}

function jobProgress(job) {
  const state = job.run.status
  if (state === 'SUCCESS') return 'Completed'
  if (state === 'RUNNING') return 'In progress'
  if (state === 'FAILED') return 'Failed'
  if (state === 'SKIPPED') return 'Skipped'
  if (state === 'CANCELLED') return 'Cancelled'
  if (state === 'NOT SELECTED') return 'Not in this execution'
  if (state === 'NOT RUN') return job.JOB_IS_ENABLED === false ? 'Disabled' : 'No executions yet'
  return 'Awaiting execution'
}

function NextRun({ workflow, job }) {
  const date = asDate(workflow.scheduler?.nextRun)
  if (!workflow.IS_SCHEDULE_ENABLED || !workflow.IS_ENABLED || job?.JOB_IS_ENABLED === false || job?.runOnly) return <span className="om-muted">—</span>
  if (!date) return <span className="om-muted">{workflow.scheduler?.state === 'STARTED' ? 'Not reported yet' : workflow.scheduler?.state === 'SUSPENDED' ? 'Paused' : 'Unavailable'}</span>
  return <span title={workflow.scheduler.nextRun}>{timeFormatter.format(date)}<small>{dateFormatter.format(date)}{job && ' · With workflow'}</small></span>
}

function ScheduleDialog({ workflow, busy, error, onClose, onSave }) {
  const [cron, setCron] = useState(workflow.SCHEDULE || '')
  const [timezone, setTimezone] = useState(workflow.SCHEDULE_TIMEZONE || 'UTC')
  const [enabled, setEnabled] = useState(workflow.forceEnabled ?? workflow.IS_SCHEDULE_ENABLED)
  return <EditorDialog title={`Schedule for ${workflow.WORKFLOW_NAME}`} busy={busy} onClose={onClose}>
    <form className="vision-modal edit-form" onSubmit={event => { event.preventDefault(); onSave({ enabled, cron, timezone, expectedEnabled: workflow.IS_SCHEDULE_ENABLED }) }}>
      <div className="modal-header"><div><h2>Workflow schedule</h2><p>{workflow.WORKFLOW_NAME}</p></div><button type="button" className="modal-close" aria-label="Close dialog" disabled={busy} onClick={onClose}>×</button></div>
      {error && <div className="alert error" role="alert">{error}</div>}
      <div className="form-field"><label htmlFor="monitor-cron">Cron expression</label><input id="monitor-cron" required={enabled} value={cron} onChange={event => setCron(event.target.value)} placeholder="0 7 * * 1-5" /></div>
      <div className="form-field"><label htmlFor="monitor-timezone">Schedule timezone</label><input id="monitor-timezone" required value={timezone} onChange={event => setTimezone(event.target.value)} placeholder="Europe/Stockholm" /></div>
      <label className="om-schedule-check"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />Enable schedule</label>
      <p className="form-help">Scheduled runs execute all enabled jobs in order. Disabling the workflow pauses its schedule. Accepted runs keep their saved job selection.</p>
      <div className="modal-actions"><button type="button" className="button" disabled={busy} onClick={onClose}>Cancel</button><button type="submit" className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save schedule'}</button></div>
    </form>
  </EditorDialog>
}

function workflowDetail(workflow) {
  return [workflow.run.message, workflow.WORKFLOW_GROUP || 'Ungrouped', !workflow.IS_ENABLED && 'Disabled',
    workflow.run.runId && workflow.run.mode?.replaceAll('_', ' '), workflow.run.requestedBy,
    workflow.parentWorkflowName && `After ${workflow.parentWorkflowName}`,
    workflow.queuedCount > 0 && `${workflow.queuedCount} queued`].filter(Boolean).join(' · ')
}

function jobDetail(job) {
  return [job.runOnly && 'No longer assigned', job.JOB_IS_ENABLED === false && 'Disabled', job.run.message || jobProgress(job)].filter(Boolean).join(' · ')
}

function WorkflowRows({ workflow, expanded, onToggle, onHistory, onRun, onDag, onLog, onState, onSchedule, onEditSchedule, busy, now }) {
  const { total, succeeded, failed, skipped, cancelled = 0, completed = succeeded + failed + skipped + cancelled } = workflow.progress
  const percent = workflow.progress.percent ?? (total ? Math.round(completed / total * 100) : 0)
  const progressLabel = `${completed} / ${total} finished · ${succeeded} succeeded${failed ? ` · ${failed} failed` : ''}${skipped ? ` · ${skipped} skipped` : ''}${cancelled ? ` · ${cancelled} cancelled` : ''}`
  const runDisabled = !workflow.canRun || activeWorkflowStatuses.has(workflow.run.status)
  const runReason = activeWorkflowStatuses.has(workflow.run.status) ? 'Workflow is already queued or running.' : workflow.runUnavailable
  const bodyId = `om-jobs-${workflow.WORKFLOW_ID}`
  const jobById = new Map(workflow.jobs.map(job => [job.JOB_ID, job]))
  const assignedCount = workflow.jobs.filter(job => !job.runOnly).length
  return <>
    <tbody className="om-parent">
      <tr className={!workflow.IS_ENABLED ? 'om-row-disabled' : undefined}>
        <th scope="row">
          <div className="om-name">
            <HierarchyToggle expanded={expanded} onClick={onToggle} aria-controls={bodyId} aria-label={`${expanded ? 'Hide' : 'Show'} jobs for ${workflow.WORKFLOW_NAME}`} />
            <div className="om-name-text"><button type="button" className="om-workflow-link" aria-label={`Execution history for ${workflow.WORKFLOW_NAME}`} onClick={onHistory}><strong title={workflow.WORKFLOW_NAME}>{workflow.WORKFLOW_NAME}</strong></button><small className={workflow.run.message ? 'om-name-detail om-error' : 'om-name-detail'} title={workflowDetail(workflow)}>{workflowDetail(workflow)}</small></div>
            <RunActions name={`workflow ${workflow.WORKFLOW_NAME}`} displayName={workflow.WORKFLOW_NAME} disabled={runDisabled} reason={runReason} onRun={() => onRun()} onHistory={onHistory} onLog={() => onLog()} canLog={Boolean(workflow.run.runId)} onState={() => onState()} enabled={workflow.IS_ENABLED} busy={busy} onSchedule={onSchedule} scheduleEnabled={workflow.IS_SCHEDULE_ENABLED} onEditSchedule={onEditSchedule} />
          </div>
        </th>
        <td><span className="om-kind">Workflow</span><small>{assignedCount} assigned {assignedCount === 1 ? 'job' : 'jobs'}</small></td>
        <RunCells run={workflow.run} lastRun={workflow.lastRun} now={now} />
        <td>{total ? <div className={`om-progress ${failed ? 'has-failure' : ''}`}><span title={progressLabel}>{completed} / {total} finished</span><div className="om-track" role="progressbar" aria-label={`Workflow progress for ${workflow.WORKFLOW_NAME}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><i style={{ width: `${percent}%` }} /></div></div> : <span className="om-muted">No executions yet</span>}</td>
        <td>{workflow.IS_SCHEDULE_ENABLED ? <><span className="om-cron">{workflow.SCHEDULE}</span><small>{workflow.SCHEDULE_TIMEZONE || 'UTC'}{!workflow.IS_ENABLED && ' · Paused with workflow'}</small></> : <span className="om-muted">Not scheduled</span>}</td>
        <td className="om-time"><NextRun workflow={workflow} /></td>

      </tr>
    </tbody>
    <tbody id={bodyId} className="om-children" hidden={!expanded}>
      {workflow.jobs.map(job => <tr key={job.JOB_ID} className={!workflow.IS_ENABLED || job.JOB_IS_ENABLED === false ? 'om-row-disabled' : undefined}>
        <th scope="row"><div className="om-job-name"><span className="om-sequence" title="Configured job order">{String(job.JOB_SEQUENCE || '—').padStart(2, '0')}</span><div className="om-name-text"><strong title={job.JOB_NAME}>{job.JOB_NAME}</strong><small className={job.run.status === 'FAILED' ? 'om-name-detail om-error' : 'om-name-detail'} title={jobDetail(job)}>{jobDetail(job)}</small></div><RunActions name={`${job.JOB_NAME} in ${workflow.WORKFLOW_NAME}`} displayName={job.JOB_NAME} disabled={runDisabled || job.runOnly || job.JOB_IS_ENABLED === false} reason={job.runOnly ? 'Job is no longer assigned.' : job.JOB_IS_ENABLED === false ? 'Job is disabled.' : runReason} onRun={() => onRun(job.JOB_ID)} onRunFrom={() => onRun(job.JOB_ID, 'FROM_JOB')} onHistory={() => onHistory(job)} onDag={job.JOB_TYPE === 'DBT' ? () => onDag(job) : undefined} canDag={Boolean(job.lastRun?.runId)} onLog={() => onLog(job)} canLog={Boolean(job.run.executionId || job.lastRun?.executionId)} onState={job.runOnly ? null : () => onState(job)} enabled={job.JOB_IS_ENABLED} busy={busy} /></div></th>
        <td><span className="om-kind">{job.JOB_TYPE || '—'}</span><small>{job.JOB_LAYER || 'GENERAL'}</small></td>
        <RunCells run={job.run} lastRun={job.lastRun} now={now} />
        <td><JobProgress job={job} /></td>
        <td><span className="om-dependency" title={job.runOnly ? 'See execution history' : job.PARENT_JOB_ID ? `After ${jobById.get(job.PARENT_JOB_ID)?.JOB_NAME || 'Unknown job'}` : `First job · ${workflow.WORKFLOW_NAME}`}>{job.runOnly ? 'See execution history' : job.PARENT_JOB_ID ? `After ${jobById.get(job.PARENT_JOB_ID)?.JOB_NAME || 'Unknown job'}` : `First job · ${workflow.WORKFLOW_NAME}`}</span><small title={dependencyLabel(job)}>{[workflow.IS_SCHEDULE_ENABLED && job.JOB_IS_ENABLED !== false && !job.runOnly && 'With workflow', !job.runOnly && dependencyLabel(job)].filter(Boolean).join(' · ') || '—'}</small></td>
        <td className="om-time"><NextRun workflow={workflow} job={job} /></td>
      </tr>)}
      {!workflow.jobs.length && <tr><td colSpan={8} className="om-empty-jobs">No jobs assigned.</td></tr>}
    </tbody>
  </>
}

export default function OrchestrationMonitor({ onNavigate, monitorState = null }) {
  const { data, error, loading, refreshing, refresh, acceptSubmission } = useOrchestrationMonitor()
  const [now, setNow] = useState(Date.now())
  const [search, setSearch] = useState(monitorState?.search || '')
  const [status, setStatus] = useState(monitorState?.status || '')
  const [group, setGroup] = useState(monitorState?.group || '')
  const [expanded, setExpanded] = useState(() => new Set(monitorState?.expanded || []))
  const [view, setView] = useState(() => monitorState?.view || (new URLSearchParams(window.location.search).get('view') === 'timeline' ? 'timeline' : 'table'))
  const [timelineRefresh, setTimelineRefresh] = useState(0)
  const [runSelection, setRunSelection] = useState(null)
  const [scheduleSelection, setScheduleSelection] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [notice, setNotice] = useState('')
  const savingRef = useRef(false)
  const rows = useMemo(() => (data?.rows || []).map(workflow => applySubmission(workflow, data?.submissions)), [data])
  const summary = useMemo(() => rows.reduce((counts, workflow) => {
    const state = ['SUBMITTING', 'INITIATING', 'CLAIMED'].includes(workflow.run.status) ? 'RUNNING' : workflow.run.status
    counts[state] = (counts[state] || 0) + 1
    return counts
  }, {}), [rows])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const visible = useMemo(() => rows.filter(workflow => matches(workflow, search.trim().toLowerCase()) && (!status || workflow.run.status === status) && (!group || workflow.WORKFLOW_GROUP === group)), [rows, search, status, group])

  function toggle(id) {
    setExpanded(current => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function changeSearch(value) {
    setSearch(value)
    if (value.trim()) setExpanded(new Set(rows.filter(workflow => matches(workflow, value.trim().toLowerCase())).map(workflow => workflow.WORKFLOW_ID)))
  }

  async function changeState(workflow, job) {
    if (savingRef.current) return
    savingRef.current = true; setSaving(true); setSaveError(''); setNotice('')
    try {
      const result = job ? await api.setOrchestrationJobState(job.JOB_ID, !job.JOB_IS_ENABLED, job.JOB_IS_ENABLED)
        : await api.setOrchestrationWorkflowState(workflow.WORKFLOW_ID, !workflow.IS_ENABLED, workflow.IS_ENABLED)
      setNotice(result.warning || `${job ? job.JOB_NAME : workflow.WORKFLOW_NAME} ${result.enabled ? 'enabled' : 'disabled'}.${job ? ' Applies wherever this job is used.' : ''}`)
      refresh()
    } catch (failure) { setSaveError(failure.message) }
    finally { savingRef.current = false; setSaving(false) }
  }

  async function saveSchedule(workflow, payload) {
    if (savingRef.current) return
    savingRef.current = true; setSaving(true); setSaveError(''); setNotice('')
    try {
      const result = await api.setOrchestrationSchedule(workflow.WORKFLOW_ID, payload)
      setNotice(result.warning || `${workflow.WORKFLOW_NAME} schedule ${payload.enabled ? 'enabled' : 'disabled'}.`)
      setScheduleSelection(null); refresh()
    } catch (failure) { setSaveError(failure.message) }
    finally { savingRef.current = false; setSaving(false) }
  }

  function toggleSchedule(workflow) {
    if (!workflow.IS_SCHEDULE_ENABLED && !workflow.SCHEDULE) { setSaveError(''); setScheduleSelection({ ...workflow, forceEnabled: true }); return }
    saveSchedule(workflow, { enabled: !workflow.IS_SCHEDULE_ENABLED, expectedEnabled: workflow.IS_SCHEDULE_ENABLED })
  }

  function openDetail(workflow, job, kind) {
    const runId = (kind === 'dag' && job ? job.lastRun?.runId : job ? job.run.runId || job.lastRun?.runId : workflow.run.runId) || ''
    onNavigate(kind === 'log' ? 'executionLog' : 'dag', { workflowId: workflow.WORKFLOW_ID,
      workflowName: job ? `${workflow.WORKFLOW_NAME} / ${job.JOB_NAME}` : workflow.WORKFLOW_NAME,
      runId, returnPage: 'orchestrationMonitor',
      orchestration: { runId, jobId: job?.JOB_ID || '', latest: kind === 'dag' } })
  }

  return <section className="page om-page">
    <PageHeader breadcrumb="Workflow / Monitor" title="Workflow Monitor" subtitle="Workflow and job execution status." actions={<button className="button" type="button" disabled={refreshing} onClick={() => { refresh(); setTimelineRefresh(value => value + 1) }}>{refreshing ? 'Refreshing…' : '↻ Refresh'}</button>} />
    {error && <div className="alert error" role="alert">{error}{data && ' Showing the last successful refresh.'}</div>}
    {saveError && !scheduleSelection && <div className="alert error" role="alert">{saveError}</div>}
    {notice && <div className="alert info" role="status">{notice}</div>}
    {data?.submissions?.filter(row => row.error).map(row => <div className="alert error" role="alert" key={row.requestId}>{rows.find(workflow => workflow.WORKFLOW_ID === row.workflowId)?.WORKFLOW_NAME || 'Workflow'}: {row.error}</div>)}
    {loading && <LoadingState>Loading workflows and executions…</LoadingState>}
    {data && !data.warming && <>
      {data.warnings?.length > 0 && <details className="om-warnings"><summary>{data.warnings.length} {data.warnings.length === 1 ? 'issue' : 'issues'} to review</summary><ul>{data.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
      <div className="monitor-toolbar om-toolbar">
        <input className="search-input" type="search" aria-label="Search workflows, groups or jobs" placeholder="Search workflows, groups or jobs…" value={search} onChange={event => changeSearch(event.target.value)} />
        <div className="monitor-toolbar-actions">
          <div className="om-view-toggle" role="group" aria-label="Monitor view"><button type="button" aria-pressed={view === 'table'} onClick={() => setView('table')}>Table</button><button type="button" aria-pressed={view === 'timeline'} onClick={() => setView('timeline')}>Timeline</button></div>
          <select className="status-select" aria-label="Filter workflow group" value={group} onChange={event => setGroup(event.target.value)}><option value="">All groups</option>{[...new Set(rows.map(workflow => workflow.WORKFLOW_GROUP).filter(Boolean))].sort().map(value => <option key={value}>{value}</option>)}</select>
          <select className="status-select" aria-label="Filter workflow status" value={status} onChange={event => setStatus(event.target.value)}><option value="">All workflow statuses</option>{statuses.map(value => <option key={value}>{value}</option>)}</select>
        </div>
      </div>
      <div className="monitor-system-strip om-summary" role="group" aria-label="Workflow summary">
        <div className="system-counts">
          {summaryStatuses.filter(([value], index) => index < 5 || summary[value]).map(([value, tone, icon, label]) => <span className={`summary-item ${tone}`} key={value}>
            <span className="summary-icon" aria-hidden="true">{icon}</span><strong>{summary[value] || 0}</strong><span>{label}</span>
          </span>)}
          <span className="summary-total"><strong>{rows.length}</strong> total</span>
          {visible.length !== rows.length && <span className="om-summary-filtered">{visible.length} shown</span>}
        </div>
        <span className="summary-updated" title="Europe/Stockholm">Updated {formatDateTime(data.generatedAt)}</span>
      </div>
      <div className="om-list-heading"><span>Workflow / Job hierarchy</span><div><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(current => new Set([...current, ...visible.map(workflow => workflow.WORKFLOW_ID)]))}>Expand all</button><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(new Set())}>Collapse all</button></div></div>
      {view === 'timeline' ? <OrchestrationTimeline workflows={visible} expanded={expanded} onToggle={toggle} now={now} liveGeneratedAt={data.generatedAt} refreshVersion={timelineRefresh} onNavigate={onNavigate} initialState={monitorState?.timeline} monitorFilters={{ search, status, group, expanded: [...expanded] }} onWorkflowControls={workflowId => { setView('table'); setExpanded(current => new Set([...current, workflowId])) }} /> : <div className="om-table-wrap">
        <table className="om-table om-monitor-table">
          <caption className="visually-hidden">Live workflows with expandable jobs. Times are in Europe/Stockholm. Each workflow and its jobs share the displayed execution.</caption>
          <colgroup><col className="om-name-col" /><col /><col /><col /><col /><col /><col className="om-trigger-col" /><col className="om-next-col" /></colgroup>
          <thead><tr>{['Workflow / Job', 'Type / Layer', 'Run status', 'Last Run', 'Duration', 'Job progress', 'Schedule', 'Next Run'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
          {visible.map(workflow => <WorkflowRows key={workflow.WORKFLOW_ID} workflow={workflow} expanded={expanded.has(workflow.WORKFLOW_ID)} onToggle={() => toggle(workflow.WORKFLOW_ID)} onHistory={job => onNavigate('orchestrationHistory', { workflowId: workflow.WORKFLOW_ID, workflowName: workflow.WORKFLOW_NAME, jobId: job?.JOB_ID || '', jobName: job?.JOB_NAME || '' })} onRun={(jobId, mode) => setRunSelection({ workflow, jobId, mode })} now={now} busy={saving || !data.runsEnabled} onState={job => changeState(workflow, job)} onSchedule={() => toggleSchedule(workflow)} onEditSchedule={() => { setSaveError(''); setScheduleSelection(workflow) }} onDag={job => openDetail(workflow, job, 'dag')} onLog={job => openDetail(workflow, job, 'log')} />)}
          {!visible.length && <tbody><tr><td colSpan={8} className="om-empty-jobs">{rows.length ? 'No workflows match your filters.' : 'No workflows configured. Add workflows and jobs on the Workflow page.'}</td></tr></tbody>}
        </table>
      </div>}
      <p className="om-footnote">{view === 'timeline' ? 'Timeline shows executions overlapping the selected window, with jobs grouped under their workflows. Filters use the current workflow status and configuration. Execution details use saved snapshots. Live follows the clock and refreshes history every 15 seconds. Range controls the loaded period; Zoom controls how much time fits on screen. Scroll horizontally to browse the full period. Overlapping runs use separate lanes.' : 'Hierarchy follows current job configuration. Statuses and progress belong to one execution per workflow: active first, then queued, then latest completed. Unselected and disabled jobs are skipped. Older executions may show Not selected. Last Run and Duration show the latest started execution for that row. Latest DAG Run opens the job’s latest started execution. Log opens the displayed execution. Click a workflow name for execution history.'}</p>
    </>}
    {runSelection && <RunWorkflowDialog workflow={rows.find(row => row.WORKFLOW_ID === runSelection.workflow.WORKFLOW_ID) || runSelection.workflow} jobId={runSelection.jobId} jobs={runSelection.workflow.jobs.filter(job => !job.runOnly)} onClose={() => setRunSelection(null)} initialMode={runSelection.mode} onAccepted={receipt => { acceptSubmission(receipt); setExpanded(current => new Set([...current, runSelection.workflow.WORKFLOW_ID])); setRunSelection(null); refresh() }} />}
    {scheduleSelection && <ScheduleDialog workflow={scheduleSelection} busy={saving} error={saveError} onClose={() => setScheduleSelection(null)} onSave={payload => saveSchedule(scheduleSelection, payload)} />}
  </section>
}
