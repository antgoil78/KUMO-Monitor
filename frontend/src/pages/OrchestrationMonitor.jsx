import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, configurationApi } from '../api.js'
import ConfigurationSource, { useConfigurationSource } from '../components/ConfigurationSource.jsx'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatDateTimeSeconds, lastRunDay } from '../utils/time.js'
import EditorDialog from '../components/EditorDialog.jsx'
import HierarchyToggle from '../components/HierarchyToggle.jsx'
import ActionIcon from '../components/ActionIcon.jsx'
import OrchestrationTimeline from '../components/OrchestrationTimeline.jsx'
import TaskEngineTrial from '../components/TaskEngineTrial.jsx'
import useOrchestrationMonitor, { activeWorkflowStatuses, applySubmission } from '../utils/useOrchestrationMonitor.js'
import { RunWorkflowDialog } from './WorkflowExecution.jsx'
import { directWaits, overlapWaits, matchesActivity, planRequests, requestProgress, requestStatus } from '../utils/monitorActivity.js'
import './OrchestrationMonitor.css'

const statuses = ['RUNNING', 'FAILED', 'SUCCESS', 'WARNING', 'SKIPPED', 'CANCELLED', 'NO RUN', 'UNKNOWN']
const resultStatuses = ['SUCCESS', 'WARNING', 'FAILED', 'CANCELLED', 'SKIPPED', 'NO RESULT']
const summaryStatuses = [
  ['SUCCESS', 'success', '✓', 'success'], ['FAILED', 'failed', '×', 'failed'],
  ['RUNNING', 'running', '▶', 'running'],
  ['NO RUN', 'muted', '—', 'no run'], ['WARNING', 'warning', '!', 'warning'],
  ['SKIPPED', 'skipped', '↷', 'skipped'], ['CANCELLED', 'muted', '—', 'cancelled'],
  ['UNKNOWN', 'warning', '!', 'needs review']
]
const timeFormatter = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit', second: '2-digit' })
const dateFormatter = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Stockholm', day: '2-digit', month: 'short', year: 'numeric' })

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

function executionOf(workflow) { return workflow.execution || workflow.run }
function resultOf(workflow) { return workflow.source === 'POSTGRES' ? workflow.lastResult || { status: 'NO RESULT' } : executionOf(workflow) }

function RunCells({ run, lastRun, result, now, stateCell = null, legacyWaiting = false }) {
  const displayed = result || run
  const timing = lastRun || run
  const start = asDate(timing.start)
  const day = lastRunDay(timing.start, now)
  const waiting = legacyWaiting && ['INITIATING', 'CLAIMED', 'QUEUED'].includes(run.status) && !run.start
  const requested = asDate(run.requestedAt)
  const phase = waiting ? `${run.status === 'INITIATING' && !run.executionId ? 'Submitting request' : 'Waiting to start'}${requested ? ` · ${Math.max(0, Math.floor((now - requested) / 1000))}s` : ''}` : ''
  return <><td className="om-execution-status" title={result ? result.end ? `Last completed execution: ${formatDateTimeSeconds(result.end)}` : 'No execution has finished yet' : undefined}><span className="om-live-status">{!result && run.status === 'RUNNING' && <span className="om-spinner" role="img" aria-label="Execution in progress" />}<StatusBadge status={displayed.status === 'NOT RUN' ? 'NO RUN' : displayed.status} /></span>{phase && <small>{phase}</small>}</td><td className="om-planning-cell">{stateCell || <span className="om-muted">—</span>}</td><td className="om-time" title={timing.start || ''}>{start ? <>{timeFormatter.format(start)}<small className={day.tone ? `om-day-${day.tone}` : ''}>{day.label}</small></> : '—'}</td><td className="om-time">{durationLabel(run, now)}</td></>
}

function PlanningState({ workflow }) {
  const plan = workflow.planning
  if (!plan) return <span className="om-muted">—</span>
  const status = plan.status
  const dependencies = directWaits(plan).filter(item => item.relationship === 'PARENT')
  const preparing = ['INITIATING', 'QUEUED'].includes(plan.status) && !plan.dispatch?.canRetry
  const waitingTitle = `${plan.waitingCount || 0} execution requests waiting for this workflow`
  const recovery = plan.recovery
  return <div className={`om-planning${plan.status === 'BLOCKED' || plan.status === 'NEEDS REVIEW' || dependencies.some(item => item.statuses.has('UNKNOWN')) ? ' om-error' : ''}`} title={plan.message || undefined}>
    <span className="om-planning-status">{status === 'RUNNING' && <span className="om-spinner" role="img" aria-label="Execution in progress" />}{preparing && <span className="om-spinner" role="img" aria-label="Preparing execution" />}{status && <span title={status === 'WAITING' ? waitingTitle : undefined}><StatusBadge status={status} count={status === 'WAITING' ? plan.waitingCount : undefined} /></span>}{plan.waitingCount > 0 && status !== 'WAITING' && <span title={waitingTitle}><StatusBadge status="WAITING" count={plan.waitingCount} /></span>}{plan.scheduled && status !== 'SCHEDULED' && <StatusBadge status="SCHEDULED" />}</span>
    {plan.queuedCount > 0 && plan.status !== 'QUEUED' && <small>{plan.queuedCount} queued</small>}
    {dependencies.length > 0 && <small className="om-dependency-list"><DependencyWaits items={dependencies} /></small>}
    {plan.stopped && <small>Plan stopped</small>}
    {plan.dispatch?.canRetry && <small className="om-dispatch-paused">Group dispatch paused</small>}
    {recovery && <small>{recovery.automatic ? 'Automatic recovery scheduled' : 'Task recovery paused'}{recovery.nextRetryAt && ` · Next connection retry ${formatDateTimeSeconds(recovery.nextRetryAt)}`}</small>}
    {recovery && plan.message && <small>{plan.message}</small>}
    {workflow.cancelRequested && <small>{plan.message || 'Cancellation requested'}</small>}
    {workflow.canRunWithChildren === false && workflow.runUnavailable && !workflow.canRun && !plan.message && plan.status === 'NO PLAN' && <small>{workflow.runUnavailable}</small>}
  </div>
}

function RunActions({ name, displayName, disabled, reason, onRun, onHistory, onRunFrom, onDag, canDag, onLog, canLog, onState, enabled, busy, onSchedule, scheduleEnabled, onEditSchedule, onCancel }) {
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
        {onCancel && <button type="button" disabled={busy} onClick={() => act(onCancel)}><ActionIcon name="pause" /><span>Cancel execution group</span></button>}
        <button type="button" disabled={busy || !onState} onClick={() => act(onState)}><ActionIcon name={enabled ? 'pause' : 'power'} /><span>{enabled ? 'Disable' : 'Enable'} {onRunFrom ? 'job' : 'workflow'}</span></button>
        {onSchedule && <><button type="button" disabled={busy} onClick={() => act(onSchedule)}><ActionIcon name={scheduleEnabled ? 'calendarOff' : 'calendar'} /><span>{scheduleEnabled ? 'Disable schedule' : 'Enable schedule'}</span></button><button type="button" disabled={busy} onClick={() => act(onEditSchedule)}><ActionIcon name="edit" /><span>Edit schedule…</span></button></>}
      </div>
    </div>, document.body)}
  </div>
}

function JobProgress({ job }) {
  const progress = job.progress
  if (!progress?.total) return <span className="om-muted">{job.run.status === 'RUNNING' && job.JOB_TYPE === 'DBT' ? job.run.source === 'POSTGRES' ? 'DBT task running' : 'Waiting for dbt progress…' : jobProgress(job)}</span>
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
  if (['NOT RUN', 'NO RUN'].includes(state)) return job.JOB_IS_ENABLED === false ? 'Disabled' : 'No executions yet'
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

function DependencyWaits({ items }) {
  const labels = { PARENT: 'Parent', CHILD: 'Child', SELF: 'Other run of' }
  return items.map(item => <span className="om-dependency-item" key={`${item.relationship}:${item.nodeKey || item.name}:${item.cascadeId || ''}`} title={item.reason || undefined}>
    <span>{labels[item.relationship]} {item.name}</span>
    {[...item.statuses].map(status => <StatusBadge key={status} status={status} showIcon={false} />)}
  </span>)
}

function jobDetail(job, dependencyWait = false) {
  return [job.runOnly && 'No longer assigned', job.JOB_IS_ENABLED === false && 'Disabled',
    dependencyWait && job.run.status === 'PENDING' ? jobProgress(job) : job.run.message || jobProgress(job)].filter(Boolean).join(' · ')
}

function JobState({ workflow, job }) {
  const current = job.run.status
  const state = current === 'UNKNOWN' ? 'NEEDS REVIEW' : current === 'RUNNING' ? 'RUNNING'
    : current === 'QUEUED' ? 'QUEUED' : current === 'PENDING' ? 'WAITING'
    : !job.runOnly && job.JOB_IS_ENABLED !== false && workflow.planning?.status !== 'RUNNING' ? workflow.planning?.status || 'NO PLAN' : 'NO PLAN'
  return <span className="om-planning-status">{state === 'RUNNING' && <span className="om-spinner" role="img" aria-label="Execution in progress" />}<StatusBadge status={state} /></span>
}

function EnabledIndicator({ enabled, kind }) {
  return <span className={`om-enabled ${enabled ? 'enabled' : 'disabled'}`} role="img" aria-label={`${kind} ${enabled ? 'enabled' : 'disabled'}`} title={enabled ? 'Enabled' : 'Disabled'} />
}

function ConfiguredTriggers({ workflow, job }) {
  if (job) return job.runOnly ? <span className="om-muted">See execution history</span> : <span>{workflow.JOB_EXECUTION_MODE === 'PARALLEL' ? 'With workflow · Parallel job' : job.PARENT_JOB_ID ? `After ${workflow.jobs.find(parent => parent.JOB_ID === job.PARENT_JOB_ID)?.JOB_NAME || 'previous job'}` : 'With workflow'}<small>{dependencyLabel(job)}</small></span>
  const parents = workflow.configuredParents || []
  return <>
    {parents.map(parent => <div className="om-configured-parent" key={parent.id}><span>Parent {parent.name}{parent.enabled === false && ' (disabled)'}</span><small>{parent.allow === false ? 'Blocked by ALLOW' : `On ${['success', 'warning', 'error'].filter(status => parent[status] !== false).join(' / ') || 'no allowed parent result'}`}{parent.parentMustBeNewer && ' · Newer result required'}{parent.sql && ' · SQL condition'}</small></div>)}
    {workflow.SCHEDULE && <div className="om-next-schedule"><span className="om-cron">{workflow.SCHEDULE}</span><small>{workflow.SCHEDULE_TIMEZONE || 'UTC'}{!workflow.IS_SCHEDULE_ENABLED ? ' · Paused' : !workflow.IS_ENABLED ? ' · Workflow disabled' : ''}</small>{workflow.IS_SCHEDULE_ENABLED && workflow.IS_ENABLED && <small>Next run <NextRun workflow={workflow} /></small>}</div>}
    {!parents.length && !workflow.SCHEDULE && <span className="om-muted">Not scheduled</span>}
  </>
}

function CompactExecutionCells({ workflow, job, now }) {
  const requests = planRequests(workflow, job)
  const result = (job || workflow).lastResult || { status: 'NO RESULT' }
  const start = asDate(result.start), day = lastRunDay(result.start, now)
  const running = requests.filter(request => request.start && !request.end)
  return <>
    <td className="om-planning-cell" aria-label={`Status for ${job?.JOB_NAME || workflow.WORKFLOW_NAME}`}>
      {requests.map((request, index) => {
        const status = requestStatus(request)
        const parents = directWaits(request).filter(item => item.relationship === 'PARENT')
        const progress = requestProgress(request)
        const overlaps = new Map()
        for (const item of overlapWaits(request).filter(item => item.relationship === 'PARENT')) {
          const key = item.cascadeId || 'another plan'
          if (!overlaps.has(key)) overlaps.set(key, new Set())
          overlaps.get(key).add(item.name)
        }
        const preparing = status === 'QUEUED'
        const diagnostics = request.recovery || status === 'NEEDS REVIEW' || status === 'CANCELLING' || request.dispatch?.canRetry
        return <div className={`om-request om-planning${['BLOCKED', 'NEEDS REVIEW', 'FAILED'].includes(status) ? ' om-error' : ''}`} key={request.requestRunId || request.runId || request.requestId} data-request-id={request.requestId} data-plan-id={request.cascadeId} title={request.message || undefined}>
          <div className="om-request-heading">
            <span className="om-planning-status">{(status === 'STARTED' || preparing) && <span className="om-spinner" role="img" aria-label={preparing ? 'Preparing execution' : 'Execution in progress'} />}<StatusBadge status={status} /></span>
            {!job && progress.total > 0 && <div className={`om-progress${progress.failed ? ' has-failure' : ''}`}><span>{progress.finished} / {progress.total} finished</span><div className="om-track" role="progressbar" aria-label={`Workflow progress for ${workflow.WORKFLOW_NAME}, request ${index + 1}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.percent}><i style={{ width: `${progress.percent}%` }} /></div></div>}
          </div>
          <small className="om-request-meta">{request.cascadeId ? <span className="om-request-number" title={`Plan ${request.cascadeId}`}>Plan {request.cascadeId.slice(0, 8)} · </span> : requests.length > 1 && <span className="om-request-number">#{index + 1} · </span>}{request.requestedAt ? <span title={formatDateTimeSeconds(request.requestedAt)}>Requested {timeFormatter.format(asDate(request.requestedAt))}</span> : 'Accepted request'}{request.requestedBy && ` · ${request.requestedBy}`}</small>
          {parents.length > 0 && <small className="om-dependency-list">{['PLANNED', 'BLOCKED', 'QUEUED'].includes(status) ? 'Waiting for ' : 'Parents: '}<DependencyWaits items={parents} /></small>}
          {[...overlaps].map(([plan, names]) => <small className="om-plan-overlap" key={plan} title={`Waiting for plan ${plan}`}>Waiting for {plan === 'another plan' ? plan : `plan ${plan.slice(0, 8)}`} · {[...names].join(', ')}</small>)}
          {job && status === 'STARTED' && <JobProgress job={{ ...job, run: request, progress: null }} />}
          {request.recovery && <small>{request.recovery.automatic ? 'Automatic recovery scheduled' : 'Task recovery paused'}{request.recovery.nextRetryAt && ` · Next connection retry ${formatDateTimeSeconds(request.recovery.nextRetryAt)}`}</small>}
          {request.dispatch?.canRetry && <small className="om-dispatch-paused">Group dispatch paused</small>}
          {(diagnostics || (job && status === 'FAILED')) && request.message && <small>{request.message}</small>}
        </div>
      })}
    </td>
    <td className="om-last-run om-execution-status" title={result.end ? `Last completed execution: ${formatDateTimeSeconds(result.end)}` : 'No execution has finished yet'}>
      <div className="om-last-run-line"><span className="om-time">{start ? timeFormatter.format(start) : '—'}</span><span className="om-live-status"><StatusBadge status={result.status} /></span>{start && result.triggerSource === 'MANUAL' && <span className="om-manual-trigger">Manual</span>}</div>
      {start && <small className={day.tone ? `om-day-${day.tone}` : ''}>{day.label}</small>}
    </td>
    <td className="om-durations om-time">{start && <span>Last: {durationLabel(result, now)}</span>}{running.map((request, index) => <small key={request.requestRunId || request.runId}>Current{running.length > 1 ? ` #${index + 1}` : ''}: {durationLabel({ ...request, status: 'RUNNING' }, now)}</small>)}{!start && !running.length && <span className="om-muted">—</span>}</td>
    <td className="om-triggers">
      <ConfiguredTriggers workflow={workflow} job={job} />
    </td>
  </>
}

function WorkflowRows({ workflow, onCancel, onRecover, onRetry, expanded, onToggle, onHistory, onRun, onDag, onLog, onState, onSchedule, onEditSchedule, busy, now }) {
  const compact = workflow.source === 'POSTGRES'
  const { total, succeeded, failed, skipped, cancelled = 0, completed = succeeded + failed + skipped + cancelled } = workflow.progress
  const execution = executionOf(workflow)
  const parallel = (execution.runId ? execution.jobExecutionMode : workflow.JOB_EXECUTION_MODE) === 'PARALLEL'
  const percent = workflow.progress.percent ?? (total ? Math.round(completed / total * 100) : 0)
  const progressLabel = `${completed} / ${total} finished · ${succeeded} succeeded${failed ? ` · ${failed} failed` : ''}${skipped ? ` · ${skipped} skipped` : ''}${cancelled ? ` · ${cancelled} cancelled` : ''}`
  const runDisabled = !workflow.canRun || activeWorkflowStatuses.has(workflow.run.status)
  const runReason = workflow.run.message || workflow.runUnavailable || (activeWorkflowStatuses.has(workflow.run.status) ? 'Workflow is already queued or running.' : '')
  const bodyId = `om-jobs-${workflow.WORKFLOW_ID}`
  const jobById = new Map(workflow.jobs.map(job => [job.JOB_ID, job]))
  const assignedCount = workflow.jobs.filter(job => !job.runOnly).length
  const nameDetail = [workflow.WORKFLOW_GROUP, workflow.DESCRIPTION].filter(Boolean).join(' · ')
  const dependencies = directWaits(workflow.planning || workflow.run)
  const compactDependencies = dependencies.length > 0 && !workflow.cancelRequested && !workflow.run.recovery && !workflow.run.dispatch?.canRetry
  return <>
    <tbody className="om-parent">
      <tr className={!workflow.IS_ENABLED ? 'om-row-disabled' : undefined}>
        <th scope="row">
          <div className="om-name">
            <HierarchyToggle expanded={expanded} onClick={onToggle} aria-controls={bodyId} aria-label={`${expanded ? 'Hide' : 'Show'} jobs for ${workflow.WORKFLOW_NAME}`} />
            <div className="om-name-text"><div className="om-workflow-heading">{compact && <EnabledIndicator enabled={workflow.IS_ENABLED} kind="Workflow" />}<button type="button" className="om-workflow-link" aria-label={`Execution history for ${workflow.WORKFLOW_NAME}`} onClick={onHistory}><strong title={workflow.WORKFLOW_NAME}>{workflow.WORKFLOW_NAME}</strong></button></div>{nameDetail && <small className="om-name-detail" title={nameDetail}>{nameDetail}</small>}</div>
            <RunActions name={`workflow ${workflow.WORKFLOW_NAME}`} displayName={workflow.WORKFLOW_NAME} disabled={runDisabled} reason={runReason} onRun={() => onRun()} onHistory={onHistory} onLog={() => onLog()} canLog={Boolean(execution.runId)} onState={workflow.isUnassigned ? null : () => onState()} onCancel={onCancel} enabled={workflow.IS_ENABLED} busy={busy} onSchedule={onSchedule} scheduleEnabled={workflow.IS_SCHEDULE_ENABLED} onEditSchedule={onEditSchedule} />
          </div>
          {compact && <div className="om-recovery-actions">{workflow.run.recovery?.canRecover && <button type="button" className="button" disabled={busy} aria-label={`Recover workflow ${workflow.WORKFLOW_NAME}`} onClick={onRecover}>Recover</button>}{workflow.run.retry && <button type="button" className="button" disabled={busy || !workflow.canRun} title={workflow.canRun ? 'Retry this failed workflow and resume its blocked descendants. Completed workflows are retained.' : workflow.runUnavailable} aria-label={`Retry failed workflow ${workflow.WORKFLOW_NAME}`} onClick={onRetry}>Retry</button>}</div>}

        </th>
        {compact ? <CompactExecutionCells workflow={workflow} now={now} /> : <>
        <td><span className="om-kind">Workflow</span><small>{assignedCount} assigned {assignedCount === 1 ? 'job' : 'jobs'}{parallel ? ' · Parallel' : ''}</small>{workflow.run.recovery?.canRecover && <button type="button" className="button" disabled={busy} aria-label={`Recover workflow ${workflow.WORKFLOW_NAME}`} onClick={onRecover}>Recover</button>}{workflow.run.retry && <button type="button" className="button" disabled={busy || !workflow.canRun} title={workflow.canRun ? 'Retry this failed workflow and resume its blocked descendants. Completed workflows are retained.' : workflow.runUnavailable} aria-label={`Retry failed workflow ${workflow.WORKFLOW_NAME}`} onClick={onRetry}>Retry</button>}</td>
        <RunCells run={execution} result={workflow.source === 'POSTGRES' ? resultOf(workflow) : undefined} now={now} legacyWaiting={!workflow.planning} stateCell={<PlanningState workflow={workflow} />} />
        <td>{total ? <div className={`om-progress ${failed ? 'has-failure' : ''}`}><span title={progressLabel}>{completed} / {total} finished</span><div className="om-track" role="progressbar" aria-label={`Workflow progress for ${workflow.WORKFLOW_NAME}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><i style={{ width: `${percent}%` }} /></div></div> : <span className="om-muted">No executions yet</span>}</td>
        <td>{workflow.IS_SCHEDULE_ENABLED ? <><span className="om-cron">{workflow.SCHEDULE}</span><small>{workflow.SCHEDULE_TIMEZONE || 'UTC'}{!workflow.IS_ENABLED && ' · Paused with workflow'}</small></> : <span className="om-muted">Not scheduled</span>}</td>
        <td className="om-time"><NextRun workflow={workflow} /></td>
        </>}


      </tr>
    </tbody>
    <tbody id={bodyId} className="om-children" hidden={!expanded}>
      {workflow.jobs.map(job => <tr key={job.JOB_ID} className={!workflow.IS_ENABLED || job.JOB_IS_ENABLED === false ? 'om-row-disabled' : undefined}>
        <th scope="row"><div className="om-job-name"><span className="om-sequence" title="Configured job order">{String(job.JOB_SEQUENCE || '—').padStart(2, '0')}</span><div className="om-name-text"><strong title={job.JOB_NAME}>{job.JOB_NAME}</strong><small className={job.run.status === 'FAILED' ? 'om-name-detail om-error' : 'om-name-detail'} title={jobDetail(job)}>{compact ? `${job.JOB_TYPE || 'Job'} · ${job.JOB_LAYER || 'GENERAL'}` : jobDetail(job, compactDependencies)}</small></div>{compact && <EnabledIndicator enabled={job.JOB_IS_ENABLED !== false} kind="Job" />}<RunActions name={`${job.JOB_NAME} in ${workflow.WORKFLOW_NAME}`} displayName={job.JOB_NAME} disabled={workflow.source === 'POSTGRES' || runDisabled || job.runOnly || job.JOB_IS_ENABLED === false} reason={workflow.source === 'POSTGRES' ? 'Individual job execution is not available; start the complete dependency group.' : job.runOnly ? 'Job is no longer assigned.' : job.JOB_IS_ENABLED === false ? 'Job is disabled.' : runReason} onRun={() => onRun(job.JOB_ID)} onRunFrom={() => onRun(job.JOB_ID, 'FROM_JOB')} onHistory={() => onHistory(job)} onDag={job.JOB_TYPE === 'DBT' ? () => onDag(job) : undefined} canDag={workflow.source !== 'POSTGRES' && Boolean(job.lastRun?.runId)} onLog={() => onLog(job)} canLog={Boolean(job.run.executionId || job.lastRun?.executionId)} onState={job.runOnly ? null : () => onState(job)} enabled={job.JOB_IS_ENABLED} busy={busy} /></div></th>
        {compact ? <CompactExecutionCells workflow={workflow} job={job} now={now} /> : <>
        <td><span className="om-kind">{job.JOB_TYPE || '—'}</span><small>{job.JOB_LAYER || 'GENERAL'}</small></td>
        <RunCells run={job.run} lastRun={job.lastRun} result={workflow.source === 'POSTGRES' ? job.lastResult || { status: 'NO RESULT' } : undefined} stateCell={workflow.source === 'POSTGRES' ? <JobState workflow={workflow} job={job} /> : null} now={now} />
        <td><JobProgress job={job} /></td>
        <td><span className="om-dependency">{job.runOnly ? 'See execution history' : parallel ? 'With workflow · Parallel job' : job.PARENT_JOB_ID ? `After ${jobById.get(job.PARENT_JOB_ID)?.JOB_NAME || 'Unknown job'}` : `First job · ${workflow.WORKFLOW_NAME}`}</span><small>{[workflow.IS_SCHEDULE_ENABLED && job.JOB_IS_ENABLED !== false && !job.runOnly && 'With workflow', !job.runOnly && (parallel ? job.hasSqlCondition ? 'SQL condition' : 'Independent job' : dependencyLabel(job))].filter(Boolean).join(' · ') || '—'}</small></td>
        <td className="om-time"><NextRun workflow={workflow} job={job} /></td>
        </>}

      </tr>)}
      {!workflow.jobs.length && <tr><td colSpan={compact ? 5 : 9} className="om-empty-jobs">No jobs assigned.</td></tr>}
    </tbody>
  </>
}

export default function OrchestrationMonitor({ onNavigate, monitorState = null }) {
  const selection = useConfigurationSource()
  const { source, trial: postgres } = selection
  const { data, error, loading, refreshing, refresh, acceptSubmission } = useOrchestrationMonitor(!!source, source)
  const [now, setNow] = useState(Date.now())
  const [search, setSearch] = useState(monitorState?.search || '')
  const [status, setStatus] = useState(monitorState?.status || '')
  const [planningStatus, setPlanningStatus] = useState(monitorState?.planningStatus || '')
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
  const statusAgeSeconds = Math.max(0, Math.floor((now - Date.parse(data?.generatedAt)) / 1000))
  const statusDelayed = statusAgeSeconds > Math.max(15, (data?.refreshSeconds || 5) * 3)
  const rows = useMemo(() => (data?.rows || []).map(workflow => applySubmission(workflow, data?.submissions)), [data])
  const summary = useMemo(() => rows.filter(workflow => !workflow.isUnassigned).reduce((counts, workflow) => {
    const state = resultOf(workflow).status === 'NOT RUN' ? 'NO RUN' : resultOf(workflow).status
    counts[state] = (counts[state] || 0) + 1
    if (workflow.source === 'POSTGRES' && workflow.planning?.status === 'RUNNING') counts.RUNNING = (counts.RUNNING || 0) + 1
    if (workflow.source === 'POSTGRES' && workflow.planning?.status === 'NEEDS REVIEW') counts.UNKNOWN = (counts.UNKNOWN || 0) + 1
    return counts
  }, {}), [rows])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const visible = useMemo(() => rows.filter(workflow => matches(workflow, search.trim().toLowerCase()) && (!status || (resultOf(workflow).status === 'NOT RUN' ? 'NO RUN' : resultOf(workflow).status) === status) && (workflow.source === 'POSTGRES' ? matchesActivity(workflow, planningStatus) : !planningStatus || workflow.planning?.status === planningStatus) && (!group || workflow.WORKFLOW_GROUP === group)), [rows, search, status, group, planningStatus])

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
      let result
      if (postgres) {
        const client = configurationApi(source)
        result = job ? await client.updateOrchestrationJob(job.JOB_ID, { revision: data.configurationRevision, enabled: !job.JOB_IS_ENABLED })
          : await client.updateOrchestrationWorkflow(workflow.WORKFLOW_ID, { revision: data.configurationRevision, enabled: !workflow.IS_ENABLED })
        result = { ...result, enabled: job ? !job.JOB_IS_ENABLED : !workflow.IS_ENABLED }
      } else result = job ? await api.setOrchestrationJobState(job.JOB_ID, !job.JOB_IS_ENABLED, job.JOB_IS_ENABLED)
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
      let result
      if (postgres) {
        const settings = { enabled: payload.enabled, cron: payload.cron ?? workflow.SCHEDULE, timezone: payload.timezone ?? workflow.SCHEDULE_TIMEZONE ?? 'Europe/Stockholm' }
        result = workflow.scheduler?.id
          ? await api.updateTaskPrototypeSchedule(workflow.scheduler.id, { ...settings, expectedRevision: workflow.scheduler.revision })
          : await api.createTaskPrototypeSchedule({ ...settings, requestId: `workflow-schedule:${workflow.graphId}:${workflow.WORKFLOW_ID}`, name: workflow.WORKFLOW_NAME,
            target: { graphId: workflow.graphId, workflowId: workflow.WORKFLOW_ID } })
      } else result = await api.setOrchestrationSchedule(workflow.WORKFLOW_ID, payload)
      setNotice(result.warning || `${workflow.WORKFLOW_NAME} schedule ${payload.enabled ? 'enabled' : 'disabled'}.`)
      setScheduleSelection(null); refresh()
    } catch (failure) { setSaveError(failure.message) }
    finally { savingRef.current = false; setSaving(false) }
  }

  async function retryDispatch(failure) {
    if (savingRef.current) return
    savingRef.current = true; setSaving(true); setSaveError(''); setNotice('')
    try {
      await api.retryTaskPrototypeDispatch(failure.cascadeId)
      setNotice('Dependency dispatch queued for retry. Completed and running jobs are retained.')
      refresh()
    } catch (error) { setSaveError(error.message) }
    finally { savingRef.current = false; setSaving(false) }
  }

  async function recoverRun(workflow) {
    if (savingRef.current) return
    savingRef.current = true; setSaving(true); setSaveError(''); setNotice('')
    try {
      await api.recoverTaskPrototypeRun(workflow.run.recovery.runId)
      setNotice(`${workflow.WORKFLOW_NAME} recovery queued. Submitted tasks are checked without executing them again.`)
      refresh()
    } catch (error) { setSaveError(error.message) }
    finally { savingRef.current = false; setSaving(false) }
  }

  async function retryWorkflow(workflow) {
    if (savingRef.current) return
    savingRef.current = true; setSaving(true); setSaveError(''); setNotice('')
    try {
      await api.retryTaskPrototypeWorkflow(workflow.run.retry.cascadeId, workflow.run.retry.nodeId)
      setNotice(`${workflow.WORKFLOW_NAME} retry accepted. Blocked descendants are waiting again; completed workflows are retained.`)
      refresh()
    } catch (error) { setSaveError(error.message) }
    finally { savingRef.current = false; setSaving(false) }
  }

  function toggleSchedule(workflow) {
    if (!workflow.IS_SCHEDULE_ENABLED && !workflow.SCHEDULE) { setSaveError(''); setScheduleSelection({ ...workflow, forceEnabled: true }); return }
    saveSchedule(workflow, { enabled: !workflow.IS_SCHEDULE_ENABLED, expectedEnabled: workflow.IS_SCHEDULE_ENABLED })
  }

  function openDetail(workflow, job, kind) {
    const runId = (kind === 'dag' && job ? job.lastRun?.runId : job ? job.run.runId || job.lastRun?.runId : executionOf(workflow).runId) || ''
    onNavigate(kind === 'log' ? 'executionLog' : 'dag', { workflowId: workflow.WORKFLOW_ID,
      workflowName: job ? `${workflow.WORKFLOW_NAME} / ${job.JOB_NAME}` : workflow.WORKFLOW_NAME,
      runId, returnPage: 'orchestrationMonitor',
      orchestration: { runId, jobId: job?.JOB_ID || '', latest: kind === 'dag', source } })
  }

  return <section className="page om-page">
    <PageHeader breadcrumb="Workflow / Monitor" title="Workflow Monitor" subtitle="Workflow and job execution status." actions={<button className="button" type="button" disabled={refreshing} onClick={() => { refresh(); setTimelineRefresh(value => value + 1) }}>{refreshing ? 'Refreshing…' : '↻ Refresh'}</button>} />
    <ConfigurationSource selection={selection} disabled={saving || !!runSelection || !!scheduleSelection} revision={data?.configurationRevision} />
    {!postgres && <TaskEngineTrial />}
    {postgres && data && <div className="om-notice om-engine-status"><strong>Execution worker: {data.worker?.state}</strong>{data.dispatchErrors?.length > 0 && <strong className="om-dispatch-paused">Dependency dispatch: {data.dispatchErrors.length} paused {data.dispatchErrors.length === 1 ? 'group' : 'groups'}</strong>}<span>{data.workflowsCount} workflows · {data.jobsCount} jobs · {data.unassignedJobs || 0} unassigned</span></div>}
    {postgres && data?.dispatchErrors?.map(failure => <div className="alert error om-dispatch-error" role="alert" key={failure.cascadeId}>
      <div><strong>Dependency dispatch paused</strong><p>{failure.workflowNames.join(', ')} · Requested {formatDateTimeSeconds(failure.requestedAt)} · {failure.attempts} attempts</p><p>{failure.error || 'Dependency evaluation unavailable'}</p><p>Retry resumes pending dependency decisions. Completed and running jobs are retained.</p></div>
      <button className="button" type="button" disabled={saving || !data.runsEnabled || !failure.canRetry} aria-label={`Retry dependency dispatch for ${failure.workflowNames.join(', ')}`} onClick={() => retryDispatch(failure)}>Retry dependency dispatch</button>
    </div>)}
    {postgres && data?.scheduleMessage && <p className="om-footnote">{data.scheduleMessage}</p>}
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
          <select className="status-select" aria-label={postgres ? 'Filter workflow result' : 'Filter workflow status'} value={status} onChange={event => setStatus(event.target.value)}><option value="">{postgres ? 'All workflow results' : 'All workflow statuses'}</option>{(postgres ? resultStatuses : statuses).map(value => <option key={value}>{value}</option>)}</select>
          {postgres && <select className="status-select" aria-label="Filter workflow activity" value={planningStatus} onChange={event => setPlanningStatus(event.target.value)}><option value="">All workflow activity</option>{['STARTED', 'PLANNED', 'QUEUED', 'SUCCEEDED', 'WARNING', 'FAILED', 'BLOCKED', 'NEEDS REVIEW', 'CANCELLING', 'SKIPPED', 'CANCELLED', 'NO PLAN'].map(value => <option key={value} value={value}>{value === 'NO PLAN' ? 'No active plan' : value}</option>)}</select>}
        </div>
      </div>
      <div className="monitor-system-strip om-summary" role="group" aria-label="Workflow summary">
        <div className="system-counts">
          {(postgres ? summaryStatuses.map(item => item[0] === 'NO RUN' ? ['NO RESULT', 'muted', '—', 'no result'] : item) : summaryStatuses).filter(([value], index) => index < 5 || summary[value]).map(([value, tone, icon, label]) => <span className={`summary-item ${tone}`} key={value} title={postgres ? value === 'RUNNING' ? 'Currently running workflows' : value === 'UNKNOWN' ? 'Workflows needing review' : 'Latest completed workflow results' : undefined}>
            <span className="summary-icon" aria-hidden="true">{icon}</span><strong>{summary[value] || 0}</strong><span>{label}</span>
          </span>)}
          <span className="summary-total"><strong>{data.workflowsCount ?? rows.length}</strong> total</span>
          {visible.length !== rows.length && <span className="om-summary-filtered">{visible.length} shown</span>}
        </div>
        <span className={`summary-updated${statusDelayed ? ' om-status-delayed' : ''}`} title="Latest database status read · Europe/Stockholm">
          {statusDelayed ? 'Status delayed · ' : 'Updated '}
          {formatDateTimeSeconds(data.generatedAt)} · {statusAgeSeconds}s ago
        </span>
      </div>
      <div className="om-list-heading"><span>Workflow / Job hierarchy</span><div><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(current => new Set([...current, ...visible.map(workflow => workflow.WORKFLOW_ID)]))}>Expand all</button><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(new Set())}>Collapse all</button></div></div>
      {view === 'timeline' ? <OrchestrationTimeline source={source} workflows={visible} expanded={expanded} onToggle={toggle} now={now} liveGeneratedAt={data.generatedAt} refreshVersion={timelineRefresh} onNavigate={onNavigate} initialState={monitorState?.timeline} monitorFilters={{ search, status, planningStatus, group, expanded: [...expanded] }} onWorkflowControls={workflowId => { setView('table'); setExpanded(current => new Set([...current, workflowId])) }} /> : <div className="om-table-wrap">
        <table className={`om-table om-monitor-table${postgres ? ' om-compact-table' : ''}`}>
          <caption className="visually-hidden">Live workflows with expandable jobs. Times are in Europe/Stockholm. {postgres ? 'Status shows requests from active execution plans. Last run shows the latest completed execution and its result.' : 'Each workflow and its jobs share the displayed execution.'}</caption>
          <colgroup>{postgres ? <><col className="om-name-col" /><col className="om-state-col" /><col className="om-last-col" /><col className="om-duration-col" /><col className="om-trigger-col" /></> : <><col className="om-name-col" /><col /><col /><col className="om-state-col" /><col /><col /><col /><col className="om-trigger-col" /><col className="om-next-col" /></>}</colgroup>
          <thead><tr>{(postgres ? ['Workflow', 'Status', 'Last run', 'Duration', 'Trigger'] : ['Workflow / Job', 'Type / Layer', 'Status', 'State', 'Last Run', 'Duration', 'Job progress', 'Schedule', 'Next Run']).map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
          {visible.map(workflow => <WorkflowRows key={workflow.WORKFLOW_ID} workflow={workflow} expanded={expanded.has(workflow.WORKFLOW_ID)} onToggle={() => toggle(workflow.WORKFLOW_ID)} onHistory={job => workflow.isUnassigned ? onNavigate('orchestrationJob') : onNavigate('orchestrationHistory', { workflowId: workflow.WORKFLOW_ID, workflowName: workflow.WORKFLOW_NAME, jobId: job?.JOB_ID || '', jobName: job?.JOB_NAME || '' })} onRun={(jobId, mode) => setRunSelection({ workflow, jobId, mode })} now={now} busy={saving || !data.runsEnabled} onRecover={() => recoverRun(workflow)} onRetry={() => retryWorkflow(workflow)} onState={job => changeState(workflow, job)} onSchedule={postgres && !workflow.scheduleAvailable && !workflow.IS_SCHEDULE_ENABLED ? undefined : () => toggleSchedule(workflow)} onEditSchedule={postgres && !workflow.scheduleAvailable && !workflow.scheduler?.id ? undefined : () => { setSaveError(''); setScheduleSelection(workflow) }} onCancel={postgres && workflow.canCancel && !workflow.cancelRequested ? async () => { try { await api.cancelTaskPrototypeCascade(workflow.cascadeId); refresh() } catch (failure) { setSaveError(failure.message) } } : undefined} onDag={job => openDetail(workflow, job, 'dag')} onLog={job => openDetail(workflow, job, 'log')} />)}
          {!visible.length && <tbody><tr><td colSpan={postgres ? 5 : 9} className="om-empty-jobs">{rows.length ? 'No workflows match your filters.' : 'No workflows configured. Add workflows and jobs on the Workflow page.'}</td></tr></tbody>}
        </table>
      </div>}
      <p className="om-footnote">{view === 'timeline' ? 'Timeline shows executions overlapping the selected window, with jobs grouped under their workflows. Filters use the Monitor’s workflow results, state and configuration. Execution details use saved snapshots. Live follows the clock and refreshes history every 15 seconds. Range controls the loaded period; Zoom controls how much time fits on screen. Scroll horizontally to browse the full period. Overlapping runs use separate lanes.' : postgres ? 'Status shows each execution request while its plan is active. Planned means waiting for dependencies; Queued means waiting to execute; Started means executing. Completed entries disappear when the whole plan completes. No active plan means an empty Status cell. Last run retains the latest completed execution’s start time and result. Duration separates Last and Current. Trigger shows configured parent rules and schedules, including paused schedules. Manual labels in Last run refer to that completed execution. Log follows the latest actual execution.' : 'Status, Last Run, Duration, job progress and Log describe the latest actual execution. NO RUN means no execution has started. State describes planned work: WAITING counts execution requests, BLOCKED means the dependency plan stopped, and SCHEDULED indicates an enabled schedule. Direct parent details appear in State; hover for the full reason. Latest DAG Run opens the job’s latest started execution. Log opens the displayed execution. Click a workflow name for execution history.'}</p>
    </>}
    {runSelection && <RunWorkflowDialog workflow={rows.find(row => row.WORKFLOW_ID === runSelection.workflow.WORKFLOW_ID) || runSelection.workflow} jobId={runSelection.jobId} jobs={runSelection.workflow.jobs.filter(job => !job.runOnly)} onClose={() => setRunSelection(null)} initialMode={runSelection.mode} onAccepted={receipt => { acceptSubmission(receipt); setExpanded(current => new Set([...current, runSelection.workflow.WORKFLOW_ID])); setRunSelection(null); refresh() }} />}
    {scheduleSelection && <ScheduleDialog workflow={scheduleSelection} busy={saving} error={saveError} onClose={() => setScheduleSelection(null)} onSave={payload => saveSchedule(scheduleSelection, payload)} />}
  </section>
}
