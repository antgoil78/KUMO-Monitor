import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Background, Controls, MarkerType, MiniMap, ReactFlow } from '@xyflow/react'
import dagre from '@dagrejs/dagre'
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
const activeRunStatuses = new Set(['INITIATING', 'RUNNING', 'IN_PROGRESS', 'EXECUTING', 'QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED', 'STARTING'])
const runningRunStatuses = new Set(['RUNNING', 'IN_PROGRESS', 'EXECUTING', 'STARTING'])
const terminalRunStatuses = new Set(['SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK', 'FAILED', 'FAILURE', 'ERROR', 'CANCELLED', 'CANCELED', 'SKIPPED'])
const terminalOverlayTtlMs = 30000
const initiatingDisplayMs = 1000
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
  const status = normalizeStatus(data?.status || data?.lock?.status, 'QUEUED')
  return {
    ...(data?.lock || {}),
    lockId: data?.lock?.lockId || '',
    workflowId,
    workflowName: data?.workflowName || data?.lock?.workflowName || workflowId,
    runId: data?.runId || data?.lock?.runId || '',
    status,
    requestedAt: data?.requestedAt || data?.lock?.requestedAt || new Date().toISOString(),
    requestedBy: data?.requestedBy || data?.lock?.requestedBy || data?.actor?.displayName || data?.actor?.userName || '',
    lastStartTime: data?.lastStartTime || data?.lock?.lastStartTime || null,
    lastEndTime: data?.lastEndTime || data?.lock?.lastEndTime || null,
    message: data?.message || data?.lock?.message || '',
    updatedAt: data?.lock?.updatedAt || data?.updatedAt || Date.now(),
    eventAt: data?.rememberedAt || data?.at || data?.updatedAt || data?.lock?.updatedAt || data?.requestedAt || null,
    sequence: Number(data?.sequence || data?.lock?.sequence || 0)
  }
}
function timestampMs(value) {
  if (!value) return 0
  if (typeof value === 'number') return value
  const parsed = new Date(value).getTime()
  return Number.isNaN(parsed) ? 0 : parsed
}
function lockRequester(lock) {
  const raw = String(lock?.requestedBy || lock?.requestedByUser || '').trim()
  const normalized = raw.toUpperCase()
  if (!raw || ['CURRENT USER', 'ANOTHER USER', 'UNKNOWN'].includes(normalized)) return ''
  if (/^[A-Z0-9_]+\.[A-Z0-9_]+$/i.test(raw)) {
    return raw
      .split('.')
      .filter(Boolean)
      .map(part => part.slice(0, 1).toUpperCase() + part.slice(1).toLowerCase())
      .join(' ')
  }
  return raw
}
function lockStatusText(lock) {
  const requester = lockRequester(lock)
  if (requester) return `Requested by ${requester}`
  const status = normalizeStatus(lock?.status, 'QUEUED')
  if (status === 'INITIATING') return 'Initiating request'
  if (status === 'QUEUED') return 'Queued for dispatch'
  if (runningRunStatuses.has(status)) return 'Running'
  return 'Run in progress'
}
function isStaleAfterTerminal(previous, liveRun) {
  const previousStatus = normalizeStatus(previous?.status, '')
  const liveStatus = normalizeStatus(liveRun?.status, '')
  if (!terminalRunStatuses.has(previousStatus) || terminalRunStatuses.has(liveStatus)) return false
  const previousRunId = String(previous?.runId || '')
  const liveRunId = String(liveRun?.runId || '')
  if (!previousRunId || !liveRunId || previousRunId !== liveRunId) return false
  return Date.now() - Number(previous?.completedAt || 0) <= terminalOverlayTtlMs
}
function upsertLock(locks, lock) {
  if (!lock?.workflowId) return locks || []
  const next = new Map((locks || []).map(item => [item.workflowId, item]))
  const previous = next.get(lock.workflowId) || {}
  const now = Date.now()
  const previousStatus = normalizeStatus(previous.status, '')
  const nextStatus = normalizeStatus(lock.status, '')
  const previousRun = String(previous.runId || '')
  const nextRun = String(lock.runId || previousRun || '')
  const previousSequence = Number(previous.sequence || 0)
  const nextSequence = Number(lock.sequence || 0)
  if (previousSequence && nextSequence && previousRun === nextRun && previousSequence > nextSequence) {
    return Array.from(next.values())
  }
  const isDowngrade = previousStatus && nextStatus && previousRun === nextRun && (statusRank[previousStatus] || 0) > (statusRank[nextStatus] || 0)
  const requestedBy = lock.requestedBy || previous.requestedBy || ''
  const requestedByUser = lock.requestedByUser || previous.requestedByUser || ''
  const sequence = Math.max(previousSequence, nextSequence)
  const merged = isDowngrade
    ? { ...lock, ...previous, requestedBy, requestedByUser, sequence, status: previousStatus, updatedAt: previous.updatedAt || now }
    : { ...previous, ...lock, requestedBy, requestedByUser, sequence, status: lock.status, updatedAt: lock.updatedAt || now }
  next.set(lock.workflowId, merged)
  return Array.from(next.values())
}
function mergeLockSnapshot(previousLocks, incomingLocks) {
  const incoming = incomingLocks || []
  const keepActive = lock => {
    const status = normalizeStatus(lock.status, '-')
    return !terminalRunStatuses.has(status)
  }
  return incoming.reduce((out, lock) => upsertLock(out, lock), []).filter(keepActive)
}
function locksSignature(locks) {
  return (locks || [])
    .map(lock => [
      lock.workflowId || '',
      lock.runId || '',
      lock.status || '',
      lock.sequence || '',
      lock.requestedBy || '',
      lock.lastStartTime || '',
      lock.lastEndTime || ''
    ].join(':'))
    .sort()
    .join('|')
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

function TimelineRun({ run, workflow, nowMs, style, onViewLog }) {
  const [tooltipPosition, setTooltipPosition] = useState(null)
  const closeTimerRef = useRef(null)
  const tooltipId = useId()
  const startTime = run.START_TIME || run.REQUESTED_AT
  const requester = String(run.REQUESTED_BY || '').trim() || 'Unknown'
  const duration = elapsedDuration(run.START_TIME || run.REQUESTED_AT, run.END_TIME, run.STATUS, nowMs)

  function openTooltip(event) {
    window.clearTimeout(closeTimerRef.current)
    const rect = event.currentTarget.getBoundingClientRect()
    const tooltipWidth = 260
    const left = Math.max(12, Math.min(rect.left + rect.width / 2 - tooltipWidth / 2, window.innerWidth - tooltipWidth - 12))
    const showAbove = rect.bottom + 190 > window.innerHeight
    setTooltipPosition({ left, top: showAbove ? rect.top - 8 : rect.bottom + 8, showAbove })
  }

  function scheduleClose() {
    closeTimerRef.current = window.setTimeout(() => setTooltipPosition(null), 120)
  }

  useEffect(() => () => window.clearTimeout(closeTimerRef.current), [])

  const kind = statusKind(run.STATUS)
  const tooltip = tooltipPosition && (
    <div
      className={`timeline-run-tooltip ${tooltipPosition.showAbove ? 'above' : ''}`}
      style={{ left: tooltipPosition.left, top: tooltipPosition.top }}
      role="tooltip"
      onMouseEnter={() => window.clearTimeout(closeTimerRef.current)}
      onMouseLeave={scheduleClose}
    >
      <div className="timeline-run-tooltip-header">
        <strong>{run.STATUS || 'Unknown'}</strong>
        <span>{workflow.workflowName}</span>
      </div>
      <dl>
        <div><dt>Start date</dt><dd>{formatDateTime(startTime)}</dd></div>
        <div><dt>Execution time</dt><dd>{duration}</dd></div>
        <div><dt>Requested by</dt><dd>{requester}</dd></div>
      </dl>
      <button type="button" onClick={() => onViewLog(run, workflow)}>▤ View log</button>
    </div>
  )

  return (
    <>
      <button
        type="button"
        className={`timeline-run ${kind}`}
        style={style}
        aria-label={`${workflow.workflowName}: ${run.STATUS || 'unknown'} execution. Started ${formatDateTime(startTime)}, duration ${duration}, requested by ${requester}`}
        aria-describedby={tooltipPosition ? tooltipId : undefined}
        onMouseEnter={openTooltip}
        onMouseLeave={scheduleClose}
        onFocus={openTooltip}
        onBlur={scheduleClose}
      />
      {tooltip && createPortal(<div id={tooltipId}>{tooltip}</div>, document.body)}
    </>
  )
}

function TimelineView({ workflows, rows, loading, nowMs, rangeHours, onViewLog }) {
  const [collapsedWorkflows, setCollapsedWorkflows] = useState(() => new Set())
  const timelineViewRef = useRef(null)
  const timelineEndRatio = 0.96
  const timelineRowHeight = 50
  const rangeMs = rangeHours * 60 * 60 * 1000
  const timelineEnd = useMemo(() => {
    const workflowIds = new Set(workflows.map(workflow => String(workflow.workflowId)))
    return (rows || []).reduce((latest, row) => {
      if (!workflowIds.has(String(row.WORKFLOW_ID || ''))) return latest
      const end = timestampMs(row.END_TIME) || (statusKind(row.STATUS) === 'running' ? nowMs : timestampMs(row.START_TIME || row.REQUESTED_AT))
      return Math.max(latest, end)
    }, 0) || nowMs
  }, [workflows, rows, nowMs])
  const windowStart = timelineEnd - rangeMs
  const timelineTicks = Array.from({ length: 7 }, (_, index) => rangeHours * (1 - index / 6))
  const hierarchy = useMemo(() => {
    const byId = new Map(workflows.map(workflow => [String(workflow.workflowId), workflow]))
    const parents = new Set(workflows.filter(workflow => workflow.parentWorkflowId).map(workflow => String(workflow.parentWorkflowId)))
    const visible = workflows.filter(workflow => {
      let parentId = String(workflow.parentWorkflowId || '')
      const visited = new Set()
      while (parentId && !visited.has(parentId)) {
        if (collapsedWorkflows.has(parentId)) return false
        visited.add(parentId)
        parentId = String(byId.get(parentId)?.parentWorkflowId || '')
      }
      return true
    })
    return { parents, visible }
  }, [workflows, collapsedWorkflows])
  const workflowRows = useMemo(() => {
    const runsByWorkflow = new Map()
    for (const row of rows || []) {
      const workflowId = String(row.WORKFLOW_ID || '')
      const start = timestampMs(row.START_TIME || row.REQUESTED_AT)
      const end = timestampMs(row.END_TIME) || nowMs
      if (!workflowId || !start || end < windowStart || start > timelineEnd) continue
      const runs = runsByWorkflow.get(workflowId) || []
      runs.push({ ...row, start, end: Math.min(end, timelineEnd) })
      runsByWorkflow.set(workflowId, runs)
    }
    return hierarchy.visible.map(workflow => ({
      workflow,
      runs: runsByWorkflow.get(String(workflow.workflowId)) || []
    }))
  }, [hierarchy.visible, rows, nowMs, timelineEnd, windowStart])
  const dependencyConnections = useMemo(() => {
    const runLocations = new Map()
    workflowRows.forEach(({ runs }, rowIndex) => {
      runs.forEach(run => runLocations.set(String(run.RUN_ID || ''), { run, rowIndex }))
    })
    const connections = []
    workflowRows.forEach(({ workflow, runs }, childRowIndex) => {
      runs.forEach(childRun => {
        const parent = runLocations.get(String(childRun.PARENT_RUN_ID || ''))
        if (!parent) return
        const parentEnd = Math.min(Math.max(parent.run.end, windowStart), timelineEnd)
        const childStart = Math.min(Math.max(childRun.start, windowStart), timelineEnd)
        connections.push({
          id: `${parent.run.RUN_ID}-${childRun.RUN_ID}`,
          fromX: ((parentEnd - windowStart) / rangeMs) * 1000 * timelineEndRatio,
          toX: ((childStart - windowStart) / rangeMs) * 1000 * timelineEndRatio,
          fromY: parent.rowIndex * timelineRowHeight + timelineRowHeight / 2,
          toY: childRowIndex * timelineRowHeight + timelineRowHeight / 2,
          tone: String(childRun.TRIGGER_SOURCE || workflow.dependencyTrigger || '').toUpperCase() === 'ON_FAIL' ? 'failure' : 'success'
        })
      })
    })
    return connections
  }, [workflowRows, windowStart, timelineEnd, rangeMs])

  function barStyle(run) {
    const start = Math.max(run.start, windowStart)
    const left = ((start - windowStart) / rangeMs) * 100 * timelineEndRatio
    const rawWidth = ((Math.max(run.end, start + 60 * 1000) - start) / rangeMs) * 100 * timelineEndRatio
    return { left: `${left}%`, width: `${Math.max(rawWidth, 0.45)}%` }
  }

  function axisLabel(hoursBeforeEnd) {
    const value = new Date(timelineEnd - hoursBeforeEnd * 60 * 60 * 1000)
    const time = value.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (rangeHours < 12) return time
    return `${value.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`
  }

  function toggleWorkflow(workflowId) {
    setCollapsedWorkflows(previous => {
      const next = new Set(previous)
      const key = String(workflowId)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function collapseAllWorkflows() {
    setCollapsedWorkflows(new Set(hierarchy.parents))
  }

  function expandAllWorkflows() {
    setCollapsedWorkflows(new Set())
  }

  useEffect(() => {
    if (loading) return undefined
    const frame = window.requestAnimationFrame(() => {
      const scrollContainer = timelineViewRef.current?.parentElement
      if (scrollContainer) scrollContainer.scrollLeft = scrollContainer.scrollWidth
    })
    return () => window.cancelAnimationFrame(frame)
  }, [rangeHours, loading])

  if (loading) return <div className="empty-state">Loading timeline executions...</div>

  return (
    <div ref={timelineViewRef} className="timeline-view" style={{ width: `${Math.min(200, Math.max(100, (rangeHours / 6) * 100))}%` }}>
      <div className="timeline-head timeline-grid-row">
        <div className="timeline-workflow-heading">
          <span>Workflow</span>
          <span className="timeline-hierarchy-actions">
            <button type="button" onClick={expandAllWorkflows} title="Expand all workflows" aria-label="Expand all workflows">⊞</button>
            <button type="button" onClick={collapseAllWorkflows} title="Collapse all workflows" aria-label="Collapse all workflows">⊟</button>
          </span>
        </div>
        <div className="timeline-axis" aria-hidden="true">
          {timelineTicks.map((hours, index) => (
            <span key={index} style={{ left: `${(index / 6) * 100 * timelineEndRatio}%` }}>
              {axisLabel(hours)}
            </span>
          ))}
        </div>
      </div>
      <div className="timeline-body">
        <div className="timeline-rows">
          {dependencyConnections.length > 0 && (
            <svg
              className="timeline-dependencies"
              viewBox={`0 0 1000 ${workflowRows.length * timelineRowHeight}`}
              preserveAspectRatio="none"
              aria-label={`${dependencyConnections.length} execution dependencies`}
            >
              <defs>
                <marker id="dependency-arrow-success" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto" markerUnits="strokeWidth">
                  <path d="M0,0 L7,3.5 L0,7 z" className="dependency-arrow success" />
                </marker>
                <marker id="dependency-arrow-failure" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto" markerUnits="strokeWidth">
                  <path d="M0,0 L7,3.5 L0,7 z" className="dependency-arrow failure" />
                </marker>
              </defs>
              {dependencyConnections.map(connection => {
                const bend = Math.max(10, Math.abs(connection.toX - connection.fromX) * 0.42)
                const path = `M ${connection.fromX} ${connection.fromY} C ${connection.fromX + bend} ${connection.fromY}, ${connection.toX - bend} ${connection.toY}, ${connection.toX} ${connection.toY}`
                return <path key={connection.id} d={path} className={`dependency-line ${connection.tone}`} markerEnd={`url(#dependency-arrow-${connection.tone})`} />
              })}
            </svg>
          )}
          {workflowRows.map(({ workflow, runs }) => (
            <div className="timeline-grid-row timeline-data-row" key={workflow.workflowId}>
              <div className="timeline-workflow-label">
                <strong style={{ paddingLeft: `${Math.min(Number(workflow.indent || 0), 4) * 10}px` }}>
                  {hierarchy.parents.has(String(workflow.workflowId)) ? (
                    <button
                      type="button"
                      className="timeline-collapse-button"
                      onClick={() => toggleWorkflow(workflow.workflowId)}
                      aria-label={`${collapsedWorkflows.has(String(workflow.workflowId)) ? 'Expand' : 'Collapse'} ${workflow.workflowName}`}
                      title={`${collapsedWorkflows.has(String(workflow.workflowId)) ? 'Expand' : 'Collapse'} child workflows`}
                    >
                      {collapsedWorkflows.has(String(workflow.workflowId)) ? '▸' : '▾'}
                    </button>
                  ) : Number(workflow.indent || 0) > 0 ? <span className="timeline-tree-arrow">↳</span> : <span className="timeline-tree-spacer" />}
                  {workflow.workflowName}
                </strong>
                {workflow.parentWorkflowName ? (
                  <span className={`timeline-schedule-note dependency ${workflow.dependencyTrigger === 'ON_FAIL' ? 'failure' : 'success'}`}>
                    {workflow.dependencyTrigger === 'ON_FAIL' ? '↯ after failure' : '↳ after success'} · {workflow.parentWorkflowName}
                  </span>
                ) : workflow.taskEnabled ? (
                  <span className="timeline-schedule-note scheduled">◷ {workflow.scheduleCron || 'Scheduled'} · {workflow.scheduleTimezone || 'UTC'}</span>
                ) : (
                  <span className="timeline-schedule-note manual">Manual trigger</span>
                )}
              </div>
              <div className="timeline-track">
                {timelineTicks.map((_, index) => <i key={index} style={{ left: `${(index / 6) * 100 * timelineEndRatio}%` }} />)}
                {runs.map((run, index) => (
                  <TimelineRun
                    key={`${run.RUN_ID || index}`}
                    run={run}
                    workflow={workflow}
                    nowMs={nowMs}
                    style={barStyle(run)}
                    onViewLog={onViewLog}
                  />
                ))}
                {runs.length === 0 && <span className="timeline-empty">No runs</span>}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="timeline-legend">
        <span><i className="success" /> Success</span>
        <span><i className="failed" /> Failed</span>
        <span><i className="running" /> Running</span>
        <span><i className="queued" /> Queued</span>
        <span><i className="dependency-success" /> After success</span>
        <span><i className="dependency-failure" /> After failure</span>
        <small>Hover an execution for details and logs</small>
      </div>
    </div>
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
          <button disabled={!workflow.lastRunId} onClick={() => click('log')}>▤ View latest log</button>
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
  const dependencyLabel = workflow.dependencyTrigger === 'ON_FAIL' ? 'after failure' : 'after success'
  return (
    <tr className={`${disabled ? 'disabled-row' : ''} ${isRoot ? 'root-row' : 'child-row'} depth-row-${Math.min(depth, 6)} ${busy ? 'busy-row' : ''}`}>
      <td className="workflow-cell">
        <div className={`workflow-tree depth-${Math.min(depth, 6)}`} style={{ '--depth': depth }}>
          {depth > 0 && <span className="tree-branch" aria-hidden="true"><span>↳</span></span>}
          <span className={`workflow-title ${isRoot ? 'root' : 'child'}`}>{workflow.workflowName}</span>
          <span className={`type-chip ${type.toLowerCase()}`}>{type}</span>
          {depth > 0 && (
            <span className={`dependency-chip ${workflow.dependencyTrigger === 'ON_FAIL' ? 'failure' : 'success'}`} title={`Triggered ${dependencyLabel} of ${workflow.parentWorkflowName || 'parent workflow'}`}>
              {dependencyLabel} · {workflow.parentWorkflowName || 'parent'}
            </span>
          )}
        </div>
      </td>
      <td className="row-actions">
        <button
          className="row-manage-button"
          aria-label={`Actions for ${workflow.workflowName}`}
          title={`Actions for ${workflow.workflowName}`}
          onClick={() => onManage(view)}
        >
          <span className="manage-dot" />
          <span>Action</span>
        </button>
      </td>
      <td className="status-cell">
        <StatusBadge status={view.lastStatus} />
        {view.runLocked && <span className="run-lock-note">{lockStatusText(view.runLock)}</span>}
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

  function refreshAfterAction() {
    Promise.resolve(onSaved()).catch(err => {
      notify(`Refresh failed after update: ${err.message}`)
    })
  }

  async function completeAction(request, successMessage, failureMessage) {
    try {
      await request
      notify(successMessage)
      refreshAfterAction()
    } catch (err) {
      notify(`${failureMessage}: ${err.message}`)
    }
  }

  function save() {
    if (saving) return
    setSaving(true)
    setError(null)

    const request = api.updateWorkflow(workflowId, detail)
    notify('Saving workflow...')
    onClose()

    void completeAction(request, 'Workflow saved', 'Failed to save workflow')
  }

  function clone() {
    if (saving) return
    setSaving(true)
    setError(null)

    const request = api.cloneWorkflow(workflowId)
    notify('Cloning workflow...')
    onClose()

    void completeAction(request, 'Workflow cloned and disabled', 'Failed to clone workflow')
  }

  function remove() {
    if (saving) return
    setSaving(true)
    setError(null)

    const request = api.deleteWorkflow(workflowId)
    notify('Deleting workflow...')
    onClose()

    void completeAction(request, 'Workflow deleted', 'Failed to delete workflow')
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
  const graph = useMemo(() => {
    const nodeWidth = 190
    const nodeHeight = 64
    const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
    layout.setGraph({ rankdir: 'LR', ranksep: 90, nodesep: 34, marginx: 30, marginy: 30 })
    const uniqueNodes = Array.from(new Map(nodes.filter(node => node.id).map(node => [String(node.id), node])).values())
    const nodeIds = new Set(uniqueNodes.map(node => String(node.id)))
    const validEdges = (dag?.edges || []).filter(edge => nodeIds.has(String(edge.source)) && nodeIds.has(String(edge.target)))
    uniqueNodes.forEach(node => layout.setNode(String(node.id), { width: nodeWidth, height: nodeHeight }))
    validEdges.forEach(edge => layout.setEdge(String(edge.source), String(edge.target)))
    dagre.layout(layout)
    const flowNodes = uniqueNodes.map(node => {
      const position = layout.node(String(node.id))
      const kind = statusKind(node.status)
      return {
        id: String(node.id),
        position: { x: position.x - nodeWidth / 2, y: position.y - nodeHeight / 2 },
        data: { label: <><span className={`dag-graph-dot ${kind}`} /><span>{node.label}</span><small>{String(node.status || 'UNKNOWN')}</small></> },
        className: `dag-graph-node ${kind}`,
        sourcePosition: 'right',
        targetPosition: 'left',
        style: { width: nodeWidth, height: nodeHeight }
      }
    })
    const statusById = new Map(uniqueNodes.map(node => [String(node.id), statusKind(node.status)]))
    const flowEdges = validEdges.map((edge, index) => {
      const targetKind = statusById.get(String(edge.target)) || ''
      const color = targetKind === 'failed' ? '#ff4b6e' : '#4779c9'
      return {
        id: `dag-edge-${edge.source}-${edge.target}-${index}`,
        source: String(edge.source),
        target: String(edge.target),
        type: 'smoothstep',
        animated: targetKind === 'running',
        markerEnd: { type: MarkerType.ArrowClosed, color },
        style: { stroke: color, strokeWidth: 1.6 }
      }
    })
    return { nodes: flowNodes, edges: flowEdges }
  }, [nodes, dag?.edges])
  return (
    <Modal title="DAG run" subtitle={workflow.workflowName} onClose={onClose} wide>
      {error && <div className="alert error">{error}</div>}
      {!dag && !error && <div className="empty-state">Loading DAG...</div>}
      {dag && <>
        <div className="dag-summary"><StatusBadge status={dag.run?.STATUS || '—'} /><span>Run ID <code>{dag.run?.RUN_ID || '—'}</code></span><span>{done}/{nodes.length} completed</span>{failed > 0 && <span className="failed-text">{failed} failed</span>}</div>
        <div className="dag-progress"><ProgressBar progress={{ percent, total: nodes.length, done, failed }} status={dag.run?.STATUS} /></div>
        {nodes.length ? (
          <div className="dag-graph" aria-label="DBT execution dependency graph">
            <ReactFlow nodes={graph.nodes} edges={graph.edges} fitView fitViewOptions={{ padding: 0.18 }} minZoom={0.15} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false} elementsSelectable>
              <Background color="rgba(105, 139, 255, 0.18)" gap={22} size={1} />
              <MiniMap pannable zoomable nodeColor={node => node.className?.includes('failed') ? '#ff4b6e' : node.className?.includes('success') ? '#01b574' : node.className?.includes('running') ? '#0075ff' : '#647695'} maskColor="rgba(3, 9, 31, 0.72)" />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        ) : <div className="soft-empty">No execution progress data for this run.</div>}
        {dag.errors?.length > 0 && <div className="modal-table-wrap"><table className="workflow-table compact"><thead><tr><th>Time</th><th>Model</th><th>Error</th></tr></thead><tbody>{dag.errors.map((err, idx) => <tr key={`${err.ORIGIN}-${idx}`}><td>{formatDateTime(err.LOG_DTTM)}</td><td>{err.ORIGIN}</td><td>{String(err.MESSAGE || '').slice(0, 240)}</td></tr>)}</tbody></table></div>}
      </>}
    </Modal>
  )
}
function ActionsModal({ workflow, onClose, onAction, pendingRun }) {
  const [runOnlyThisWorkflow, setRunOnlyThisWorkflow] = useState(false)
  const view = pendingRun ? {
    ...workflow,
    lastStatus: pendingRun.status || 'INITIATING',
    lastRunId: pendingRun.runId || workflow.lastRunId,
    runLocked: !terminalRunStatuses.has(normalizeStatus(pendingRun.status, '-')),
    runLock: pendingRun
  } : workflow
  const wfEnabled = Boolean(view.workflowEnabled)
  const taskEnabled = Boolean(view.taskEnabled)
  const isDbt = String(view.workflowType || '').toUpperCase() === 'DBT'
  const runLock = view.runLock || null
  const busy = isWorkflowBusy(view.lastStatus) || Boolean(view.runLocked)
  const hasParent = Number(view.indent || 0) > 0
  const dependencyCondition = view.dependencyTrigger === 'ON_FAIL' ? 'fails' : 'succeeds'
  const parentContext = view.parentWorkflowName
    ? `Usually triggered when ${view.parentWorkflowName} ${dependencyCondition}. This starts it independently.`
    : 'This workflow has an upstream dependency. This starts it independently.'
  async function choose(action) {
    await onAction(action, view, { skipChildren: action === 'run' && runOnlyThisWorkflow })
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
      <label className="workflow-run-only-option">
        <input type="checkbox" checked={runOnlyThisWorkflow} onChange={event => setRunOnlyThisWorkflow(event.target.checked)} disabled={!wfEnabled || busy} />
        <span><strong>Run only this workflow</strong><small>Skip both ON_SUCCESS and ON_FAIL child workflows for this manual run.</small></span>
      </label>
      <div className="action-grid">
        <button className="action-tile primary" disabled={!wfEnabled || busy} onClick={() => choose('run')}>
          <span className="action-icon">▶</span>
          <strong>{busy ? 'Workflow active' : hasParent ? 'Run independently' : 'Run workflow'}</strong>
          <small>{runLock ? lockStatusText(runLock) : (busy ? 'Run is disabled while initiating, queued or running.' : (hasParent ? parentContext : 'Create a manual run request.'))}</small>
        </button>
        {isDbt && (
          <button className="action-tile" onClick={() => choose('dag')}>
            <span className="action-icon">⌘</span>
            <strong>Show DAG run</strong>
            <small>Open latest DBT execution progress.</small>
          </button>
        )}
        <button className="action-tile" disabled={!view.lastRunId} onClick={() => choose('log')}>
          <span className="action-icon">▤</span>
          <strong>View latest log</strong>
          <small>Inspect history messages, execution progress and run logs.</small>
        </button>
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
export default function Monitor({ onNavigate }) {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [actionMessage, setActionMessage] = useState(null)
  const [openMenuId, setOpenMenuId] = useState(null)
  const [pendingRuns, setPendingRuns] = useState({})
  const pendingRunsRef = useRef({})
  const [globalLocks, setGlobalLocks] = useState([])
  const [modal, setModal] = useState(null)
  const [viewMode, setViewMode] = useState('table')
  const [timelineRows, setTimelineRows] = useState([])
  const [timelineLoading, setTimelineLoading] = useState(false)
  const [timelineLoaded, setTimelineLoaded] = useState(false)
  const [timelineRangeHours, setTimelineRangeHours] = useState(24)
  useEffect(() => {
    pendingRunsRef.current = pendingRuns
  }, [pendingRuns])

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
  function applyLiveRunUpdate(data) {
    const liveRun = liveRunFromEvent(data)
    if (!liveRun) return
    const liveStatus = normalizeStatus(liveRun.status, '-')
    const liveTerminal = terminalRunStatuses.has(liveStatus)
    if (isStaleAfterTerminal(pendingRunsRef.current[liveRun.workflowId], liveRun)) return
    if (liveTerminal) {
      const previous = pendingRunsRef.current[liveRun.workflowId] || {}
      pendingRunsRef.current = {
        ...pendingRunsRef.current,
        [liveRun.workflowId]: {
          ...previous,
          runId: liveRun.runId || previous.runId || 'pending',
          status: liveRun.status,
          sequence: Math.max(Number(previous.sequence || 0), Number(liveRun.sequence || 0)),
          lastStartTime: liveRun.lastStartTime || previous.lastStartTime || null,
          lastEndTime: liveRun.lastEndTime || previous.lastEndTime || null,
          completedAt: Date.now()
        }
      }
      setGlobalLocks(prev => (prev || []).filter(lock => lock.workflowId !== liveRun.workflowId))
    } else {
      setGlobalLocks(prev => upsertLock(prev, liveRun))
    }
    setPendingRuns(prev => {
      const previous = prev[liveRun.workflowId] || {}
      const previousStatus = normalizeStatus(previous.status, '')
      const callbackLiveStatus = normalizeStatus(liveRun.status, '')
      const previousSequence = Number(previous.sequence || 0)
      const liveSequence = Number(liveRun.sequence || 0)
      if (previousSequence && liveSequence && previousSequence > liveSequence) return prev
      const previousActive = activeRunStatuses.has(previousStatus) &&
        Date.now() - Number(previous.startedAt || Date.now()) <= 120000
      const callbackLiveTerminal = terminalRunStatuses.has(callbackLiveStatus)
      const previousRunId = String(previous.runId || '')
      const liveRunId = String(liveRun.runId || '')
      const staleForPendingRun = previousActive && (
        timestampMs(liveRun.eventAt) < Number(previous.startedAt || 0) ||
        callbackLiveTerminal && (
          previousRunId === 'pending' ||
          (previousRunId && liveRunId && previousRunId !== liveRunId)
        )
      )
      if (staleForPendingRun) return prev
      const shouldHoldInitiating = previousStatus === 'INITIATING' &&
        callbackLiveStatus === 'QUEUED' &&
        Number(previous.holdUntil || 0) > Date.now()
      const next = {
        ...prev,
        [liveRun.workflowId]: {
          ...previous,
          startedAt: prev[liveRun.workflowId]?.startedAt || Date.now(),
          runId: liveRun.runId || prev[liveRun.workflowId]?.runId || 'pending',
          status: shouldHoldInitiating ? 'INITIATING' : (liveRun.status || previous.status || 'QUEUED'),
          heldStatus: shouldHoldInitiating ? liveRun.status : null,
          holdUntil: callbackLiveStatus === 'INITIATING' ? Date.now() + initiatingDisplayMs : previous.holdUntil,
          sequence: Math.max(previousSequence, liveSequence),
          lastStartTime: liveRun.lastStartTime || prev[liveRun.workflowId]?.lastStartTime || null,
          lastEndTime: liveRun.lastEndTime || prev[liveRun.workflowId]?.lastEndTime || null,
          completedAt: terminalRunStatuses.has(normalizeStatus(liveRun.status, '-')) ? Date.now() : prev[liveRun.workflowId]?.completedAt
        }
      }
      pendingRunsRef.current = next
      return next
    })
  }
  async function loadRealtimeState() {
    try {
      const data = await api.realtimeState()
      setGlobalLocks(prev => {
        const next = mergeLockSnapshot(prev, data.locks || [])
        return locksSignature(prev) === locksSignature(next) ? prev : next
      })
      for (const event of data.events || []) {
        applyLiveRunUpdate(event)
      }
    } catch (err) {
      console.warn('Could not refresh realtime state', err)
    }
  }
  useEffect(() => { load(false); loadRealtimeState() }, [])
  useEffect(() => {
    if (viewMode !== 'timeline' || timelineLoaded) return
    let cancelled = false
    setTimelineLoading(true)
    api.history(2000)
      .then(data => {
        if (!cancelled) {
          setTimelineRows(data.rows || [])
          setTimelineLoaded(true)
        }
      })
      .catch(err => !cancelled && setError(err.message))
      .finally(() => !cancelled && setTimelineLoading(false))
    return () => { cancelled = true }
  }, [viewMode, timelineLoaded])
  useEffect(() => {
    function handleRealtime(browserEvent) {
      const event = browserEvent.detail
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
    }
    window.addEventListener('kumo:realtime', handleRealtime)
    return () => window.removeEventListener('kumo:realtime', handleRealtime)
  }, [])
  useEffect(() => {
    const id = setInterval(() => loadRealtimeState(), 1000)
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
    const pendingStatus = normalizeStatus(
      pending.status === 'INITIATING' && pending.heldStatus && Number(pending.holdUntil || 0) <= Date.now()
        ? pending.heldStatus
        : pending.status,
      'INITIATING'
    )
    const pendingTerminal = terminalRunStatuses.has(pendingStatus)
    const viewStatus = normalizeStatus(view.lastStatus, '-')
    const pendingRunId = String(pending.runId || '')
    const viewRunId = String(view.lastRunId || view.runLock?.runId || '')
    const pendingSameRun = pendingRunId && pendingRunId !== 'pending' && viewRunId === pendingRunId
    const pendingIsAhead = pendingSameRun && (statusRank[pendingStatus] || 0) > (statusRank[viewStatus] || 0)
    const holdInitiating = pendingStatus === 'INITIATING' && Number(pending.holdUntil || 0) > Date.now()
    const pendingActive = activeRunStatuses.has(pendingStatus)
    const pendingExpired = Date.now() - Number(pending.startedAt || Date.now()) > 120000
    if (pendingTerminal && pendingRunId && viewRunId && !pendingSameRun) return view
    if (pendingTerminal && pending.completedAt && Date.now() - Number(pending.completedAt) > terminalOverlayTtlMs) return view
    if (pendingActive && !pendingExpired) {
      if (pendingSameRun && (runningRunStatuses.has(viewStatus) || terminalRunStatuses.has(viewStatus)) && !pendingIsAhead && !holdInitiating) {
        return view
      }
      const pendingLock = {
        ...pending,
        requestedBy: pending.requestedBy || view.runLock?.requestedBy || view.lastRequestedBy || '',
        requestedByUser: pending.requestedByUser || view.runLock?.requestedByUser || ''
      }
      return {
        ...view,
        runLocked: true,
        runLock: pendingLock,
        lastStatus: pendingStatus,
        lastRunId: pendingLock.runId || view.lastRunId,
        lastStartTime: pendingLock.lastStartTime || null,
        lastEndTime: null,
        progress: { percent: null }
      }
    }
    if (view.runLocked && !pendingIsAhead && !holdInitiating) return view
    const actualBusy = isWorkflowBusy(view.lastStatus)
    if (!pendingTerminal && !holdInitiating && ((actualBusy && !pendingIsAhead) || pendingExpired)) return view
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
  const hasActiveVisibleRuns = useMemo(
    () => workflowsWithPending.some(w => isWorkflowBusy(w.lastStatus) || w.runLocked),
    [workflowsWithPending]
  )

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), hasActiveVisibleRuns ? 1000 : 15000)
    return () => clearInterval(id)
  }, [hasActiveVisibleRuns])
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

  async function handleAction(action, workflow, options = {}) {
    setOpenMenuId(null)
    setActionMessage(null)
    if (action === 'history') {
      setModal(null)
      return onNavigate('history', {
        workflowName: workflow.workflowName,
        workflowId: workflow.workflowId
      })
    }
    if (action === 'dag') {
      setModal(null)
      return onNavigate('dag', { workflow })
    }
    if (action === 'log') {
      setModal(null)
      return onNavigate('executionLog', {
        runId: workflow.lastRunId,
        workflowName: workflow.workflowName,
        workflowId: workflow.workflowId,
        returnPage: 'monitor'
      })
    }
    if (action === 'edit') return setModal({ type: 'edit', workflow })
    setModal(null)
    try {
      if (action === 'run') {
        const result = await api.runWorkflow(workflow.workflowId, workflow.workflowName, Boolean(options.skipChildren))
        applyLiveRunUpdate({
          ...result,
          workflowId: workflow.workflowId,
          workflowName: workflow.workflowName,
        })
        notify(`Initiated ${workflow.workflowName}${options.skipChildren ? ' without child workflows' : ''}. Waiting for dispatcher pickup...`)
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
      if (action === 'run' && err.data?.lock) {
        applyLiveRunUpdate({
          workflowId: workflow.workflowId,
          workflowName: workflow.workflowName,
          status: err.data.status || err.data.lock.status || 'ACTIVE',
          lock: err.data.lock,
          requestedBy: err.data.lock.requestedBy || '',
        })
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
        <button className="button refresh-button" onClick={() => load(true)}>↻ Refresh now</button>
      </div>
      {error && <div className="alert error">{error}</div>}
      {payload?.error && <div className="alert warning">Backend fallback: {payload.error}</div>}
      {actionMessage && <div className="alert info">{actionMessage}</div>}
      <div className="monitor-toolbar">
        <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter workflows..." className="search-input" />
        <div className="monitor-toolbar-actions">
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="status-select">
            {statusOptions.map(option => <option key={option.value || 'all'} value={option.value}>{option.label}</option>)}
          </select>
          <div className="view-switch" aria-label="Monitor view">
            <button className={viewMode === 'table' ? 'active' : ''} onClick={() => setViewMode('table')}>☷ Table</button>
            <button className={viewMode === 'timeline' ? 'active' : ''} onClick={() => setViewMode('timeline')}>━ Timeline</button>
          </div>
          {viewMode === 'timeline' && (
            <label className="timeline-range-control">
              <span>Range</span>
              <select value={timelineRangeHours} onChange={event => setTimelineRangeHours(Number(event.target.value))} aria-label="Timeline range">
                <option value="6">6 hours</option>
                <option value="12">12 hours</option>
                <option value="24">24 hours</option>
                <option value="72">3 days</option>
                <option value="168">1 week</option>
                <option value="336">2 weeks</option>
                <option value="720">1 month</option>
              </select>
            </label>
          )}
        </div>
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
      <div className={`table-card monitor-table-card vision-card-flat ${viewMode === 'timeline' ? 'timeline-card' : ''}`}>
        {loading ? <div className="empty-state">Loading monitor data...</div> : null}
        {!loading && filtered.length === 0 ? <div className="empty-state">No workflows match the current filters.</div> : null}
        {filtered.length > 0 && viewMode === 'timeline' && (
          <TimelineView
            workflows={filtered}
            rows={timelineRows}
            loading={timelineLoading}
            nowMs={nowMs}
            rangeHours={timelineRangeHours}
            onViewLog={(run, workflow) => onNavigate('executionLog', {
              workflowName: workflow.workflowName,
              workflowId: workflow.workflowId,
              runId: run.RUN_ID,
              returnPage: 'monitor'
            })}
          />
        )}
        {filtered.length > 0 && viewMode === 'table' && (
          <table className="workflow-table monitor-table">
            <thead><tr><th>Workflow</th><th><span className="visually-hidden">Actions</span></th><th>Status</th><th>Last Run</th><th>Duration</th><th>Schedule</th><th>Next Run</th></tr></thead>
            <tbody>{filtered.map(w => <WorkflowRow key={w.workflowId} workflow={w} nowMs={nowMs} onManage={(workflow) => setModal({ type: 'actions', workflow })} pendingRun={pendingRuns[w.workflowId]} />)}</tbody>
          </table>
        )}
      </div>
      {modal?.type === 'actions' && <ActionsModal workflow={modal.workflow} pendingRun={pendingRuns[modal.workflow.workflowId]} onClose={() => setModal(null)} onAction={handleAction} />}
      {modal?.type === 'dag' && <DagModal workflow={modal.workflow} onClose={() => setModal(null)} />}
      {modal?.type === 'edit' && <EditModal workflowId={modal.workflow.workflowId} onClose={() => setModal(null)} onSaved={() => load(true)} notify={notify} />}
    </section>
  )
}
