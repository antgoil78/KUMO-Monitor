import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import HierarchyToggle from './HierarchyToggle.jsx'
import EditorDialog from './EditorDialog.jsx'
import DagPreviewDialog from './DagPreviewDialog.jsx'
import ActionIcon from './ActionIcon.jsx'
import StatusBadge, { statusKind } from './StatusBadge.jsx'
import { formatDateTimeSeconds, formatDuration } from '../utils/time.js'
import { MIN_SPAN, MAX_SPAN, activeTimelineStatuses, barGeometry, connectedExecutions, executionInterval, executionKey, parseStockholmInput, preciseDuration, stockholmInput, timelineLayout, timelineTicks } from '../utils/orchestrationTimeline.js'
import { refreshTimelineHistory } from '../utils/orchestrationTimelineHistory.js'
import './OrchestrationTimeline.css'

const ranges = [[60000, '1 minute'], [300000, '5 minutes'], [900000, '15 minutes'], [3600000, '1 hour'], [21600000, '6 hours'], [86400000, '24 hours'], [604800000, '7 days']]
const zooms = [[60000, '1 minute'], [300000, '5 minutes'], [900000, '15 minutes'], [3600000, '1 hour'], [21600000, '6 hours'], [43200000, '12 hours'], [86400000, '1 day'], [604800000, '7 days']]
function spanLabel(ms) { return ms % 86400000 === 0 ? `${ms / 86400000} ${ms === 86400000 ? 'day' : 'days'}` : formatDuration(ms / 1000) }
const exactTime = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Stockholm', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 })
const axisTime = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit', second: '2-digit' })
const axisDate = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Stockholm', day: '2-digit', month: 'short' })
function exact(value) { return value ? exactTime.format(new Date(value)) : '—' }

function mergeLive(history, workflows, generatedAt) {
  const runs = new Map(history.map(run => [`${run.workflowId}:${run.runId}`, run]))
  for (const workflow of workflows) {
    const current = workflow.run
    if (!current.runId) continue
    const key = `${workflow.WORKFLOW_ID}:${current.runId}`
    const previous = runs.get(key)
    if (previous && Date.parse(generatedAt || '') > Date.parse(workflow.liveGeneratedAt || '')) continue
    const jobs = workflow.jobs.filter(job => job.run.runId === current.runId && job.run.executionId).map(job => ({ ...job.run, jobId: job.JOB_ID, name: job.JOB_NAME, type: job.JOB_TYPE, layer: job.JOB_LAYER }))
    runs.set(key, { ...previous, ...current, workflowId: workflow.WORKFLOW_ID, workflowName: previous?.workflowName || workflow.WORKFLOW_NAME,
      jobs: previous ? previous.jobs.map(job => ({ ...job, ...jobs.find(live => live.executionId === job.executionId) })).concat(jobs.filter(job => !previous.jobs.some(old => old.executionId === job.executionId))) : jobs })
  }
  return [...runs.values()]
}

export default function OrchestrationTimeline({ workflows, expanded, onToggle, now, liveGeneratedAt, refreshVersion, onNavigate, initialState = null, monitorFilters, onWorkflowControls }) {
  const [span, setSpan] = useState(initialState?.span ?? 604800000)
  const [live, setLive] = useState(initialState?.live ?? true)
  const [anchor, setAnchor] = useState(initialState?.anchor ?? now)
  const [zoomSpan, setZoomSpan] = useState(initialState?.zoomSpan ?? 86400000)
  const [compact, setCompact] = useState(initialState?.compact ?? false)
  const [showRelations, setShowRelations] = useState(initialState?.showRelations ?? true)
  const markerId = useId().replaceAll(':', '')
  const [custom, setCustom] = useState(false)
  const [customStart, setCustomStart] = useState('')
  const [customEnd, setCustomEnd] = useState('')
  const [rangeError, setRangeError] = useState('')
  const [result, setResult] = useState(initialState?.history ?? null)
  const history = useRef(result)
  history.current = result
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [selection, setSelection] = useState(initialState?.selection ?? null)
  const [actionsOpen, setActionsOpen] = useState(false)
  const [preview, setPreview] = useState(null)
  const [copyNotice, setCopyNotice] = useState('')
  const [viewportWidth, setViewportWidth] = useState(1000)
  const [scrollPosition, setScrollPosition] = useState(0)
  const viewport = useRef(null)
  const previousViewport = useRef(null)
  const scrollTarget = useRef(initialState?.center ?? null)
  const generation = useRef(0)
  const viewportKey = useRef(`${live ? 'live' : anchor}:${span}`)
  const lastRefresh = useRef(refreshVersion)
  const lastRetry = useRef(0)
  const nextLiveRead = useRef(0)
  const liveQueryEnd = useRef((Math.floor(now / 15000) + 1) * 15000)
  const end = live ? now : anchor
  const start = end - span
  const rangeKey = `${live ? 'live' : anchor}:${span}`
  // Keep reads bounded and shared across clients. The live viewport moves every
  // second; historical snapshots refresh every 15 seconds, with SSE overlaid.
  // Finish a paginated read before scheduling another; slow queries must not
  // be cancelled by the next polling tick. Explicit range changes still cancel.
  if (live && (viewportKey.current !== rangeKey || (!loading && !loadingMore && now >= nextLiveRead.current))) {
    liveQueryEnd.current = (Math.floor(now / 15000) + 1) * 15000
  }
  const queryEnd = live ? liveQueryEnd.current : anchor
  const queryStart = queryEnd - span - (live ? 15000 : 0)
  const query = useMemo(() => ({ start: new Date(queryStart).toISOString(), end: new Date(queryEnd).toISOString(), days: 0, limit: 100 }), [queryStart, queryEnd])

  useEffect(() => {
    const node = viewport.current
    const observer = new ResizeObserver(([entry]) => setViewportWidth(entry.contentRect.width))
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const id = ++generation.current
    let active = true
    const force = lastRefresh.current !== refreshVersion || lastRetry.current !== retry
    lastRefresh.current = refreshVersion
    lastRetry.current = retry
    setLoading(true); setLoadingMore(false); setError('')
    // Retain history during a live refresh; clear it when the user changes range.
    const key = rangeKey
    if (viewportKey.current !== key) { history.current = null; setResult(null); viewportKey.current = key }
    const previous = history.current
    const timer = window.setTimeout(() => {
      refreshTimelineHistory(api.orchestrationHistory, query, previous, force, () => active && generation.current === id)
        .then(value => { if (active && value) setResult(value) })
        .catch(failure => { if (active) setError(failure.message) })
        .finally(() => { if (active && generation.current === id) { nextLiveRead.current = Date.now() + 15000; setLoading(false) } })
    }, 180)
    return () => { active = false; generation.current++; window.clearTimeout(timer) }
  }, [query, refreshVersion, retry])

  async function loadMore() {
    if (loading || loadingMore || !result?.nextCursor) return
    const id = generation.current
    setLoadingMore(true); setError('')
    try {
      const page = await api.orchestrationHistory({ ...query, cursor: result.nextCursor })
      if (generation.current === id) setResult(current => {
        const runs = new Map(current.runs.map(run => [`${run.workflowId}:${run.runId}`, run]))
        page.runs.forEach(run => runs.set(`${run.workflowId}:${run.runId}`, run))
        return { ...current, runs: [...runs.values()], nextCursor: page.nextCursor, pageCount: (current.pageCount || 1) + 1 }
      })
    } catch (failure) { if (generation.current === id) setError(failure.message) }
    finally { if (generation.current === id) { nextLiveRead.current = Date.now() + 15000; setLoadingMore(false) } }
  }

  const runs = useMemo(() => mergeLive(result?.runs || [], workflows.map(workflow => ({ ...workflow, liveGeneratedAt })), result?.generatedAt), [result, workflows, liveGeneratedAt])
  const visibleIds = useMemo(() => new Set(workflows.map(workflow => workflow.WORKFLOW_ID)), [workflows])
  const visibleRuns = useMemo(() => runs.filter(run => visibleIds.has(run.workflowId) && barGeometry(executionInterval(run, now), start, end)), [runs, visibleIds, now, start, end])
  const labelWidth = Math.min(compact ? 270 : 330, Math.max(180, viewportWidth * .4))
  const visibleWidth = Math.max(1, viewportWidth - labelWidth)
  // Bound the canvas at very fine scales; shorter custom ranges still support
  // second-level zoom without creating an enormous browser layout.
  const minZoom = Math.max(MIN_SPAN, span / 256)
  const visibleSpan = Math.min(span, Math.max(minZoom, zoomSpan))
  const trackWidth = visibleWidth * span / visibleSpan
  const { ticks, step } = useMemo(() => timelineTicks(start, end, trackWidth), [start, end, trackWidth])
  const rows = useMemo(() => workflows.flatMap(workflow => {
    const history = runs.filter(run => run.workflowId === workflow.WORKFLOW_ID)
    const rows = [{ key: workflow.WORKFLOW_ID, workflow, runs: history }]
    if (!expanded.has(workflow.WORKFLOW_ID)) return rows
    const jobs = new Map(workflow.jobs.map(job => [job.JOB_ID, { id: job.JOB_ID, name: job.JOB_NAME, type: job.JOB_TYPE, position: job.JOB_SEQUENCE }]))
    history.filter(run => barGeometry(executionInterval(run, now), start, end)).forEach(run => run.jobs.forEach(job => {
      if (!jobs.has(job.jobId)) jobs.set(job.jobId, { id: job.jobId, name: job.name, type: job.type, position: job.position, historical: true })
    }))
    for (const job of jobs.values()) rows.push({ key: `${workflow.WORKFLOW_ID}:${job.id}`, workflow, job,
      runs: history.flatMap(run => run.jobs.filter(child => child.jobId === job.id).map(child => ({ ...child, workflowId: run.workflowId, workflowName: run.workflowName }))) })
    return rows
  }), [workflows, runs, expanded, now, start, end])
  const selected = selection && (runs.flatMap(run => [run, ...run.jobs.map(job => ({ ...job, workflowId: run.workflowId, workflowName: run.workflowName }))])
    .find(run => run.workflowId === selection.workflowId && run.executionId === selection.executionId) || selection)
  const layout = useMemo(() => timelineLayout(rows, runs, start, end, now, trackWidth, compact), [rows, runs, start, end, now, trackWidth, compact])
  const connected = useMemo(() => connectedExecutions(runs, selection), [runs, selection])
  const selectedJob = selected?.jobId && workflows.find(workflow => workflow.WORKFLOW_ID === selected.workflowId)?.jobs.find(job => job.JOB_ID === selected.jobId && !job.runOnly)

  function syncScroll(event) {
    setScrollPosition(event.currentTarget.scrollLeft)
  }

  useLayoutEffect(() => {
    const node = viewport.current, previous = previousViewport.current
    if (Math.abs(node.clientWidth - viewportWidth) > 1) { setViewportWidth(node.clientWidth); return }
    let center = scrollTarget.current
    if (center === null && !live && previous) center = previous.start + (node.scrollLeft + previous.visibleWidth / 2) / previous.trackWidth * previous.span
    node.scrollLeft = center === null ? node.scrollWidth - node.clientWidth : (center - start) / span * trackWidth - visibleWidth / 2
    setScrollPosition(node.scrollLeft)
    scrollTarget.current = null
    previousViewport.current = { start, span, trackWidth, visibleWidth }
  }, [trackWidth, visibleWidth, span, live, anchor])

  function zoom(factor) {
    setZoomSpan(Math.max(minZoom, Math.min(span, visibleSpan * factor)))
  }
  function move(direction) {
    scrollTarget.current = start + (viewport.current.scrollLeft + visibleWidth / 2) / trackWidth * span + direction * span
    setAnchor(end + direction * span); setLive(false)
  }
  function focusRun(run) {
    const interval = executionInterval(run, now)
    if (!interval) return
    const next = Math.min(MAX_SPAN, Math.max(MIN_SPAN, (interval.end - interval.start) * 1.3))
    setSpan(next); setZoomSpan(next); setAnchor((interval.start + interval.end) / 2 + next / 2); setLive(false)
    scrollTarget.current = (interval.start + interval.end) / 2
  }
  function fit() {
    const intervals = visibleRuns.map(run => executionInterval(run, now))
    if (!intervals.length) return
    const first = Math.max(start, Math.min(...intervals.map(item => item.start))), last = Math.min(end, Math.max(...intervals.map(item => item.end)))
    setZoomSpan(Math.min(span, Math.max(minZoom, (last - first) * 1.1)))
    setAnchor(end); setLive(false); scrollTarget.current = (first + last) / 2
  }
  function applyRange(event) {
    event.preventDefault()
    const from = parseStockholmInput(customStart), to = parseStockholmInput(customEnd)
    if (from === null || to === null || to - from < MIN_SPAN || to - from > MAX_SPAN) {
      setRangeError('Choose valid Stockholm times, from 1 second to 30 days apart. Times skipped by daylight saving are unavailable.'); return
    }
    setSpan(to - from); setAnchor(to); setLive(false); setRangeError(''); setCustom(false)
  }
  function navigate(run, kind) {
    const returnContext = { monitorState: { ...monitorFilters, view: 'timeline', timeline: { span, live, anchor: end, zoomSpan, compact, showRelations, selection, history: result,
      center: start + (viewport.current.scrollLeft + visibleWidth / 2) / trackWidth * span } } }
    if (kind === 'edit' || kind === 'dependencies') {
      onNavigate(kind === 'edit' ? 'orchestrationWorkflow' : 'orchestrationDependencies', { workflowId: run.workflowId,
        workflowName: workflows.find(workflow => workflow.WORKFLOW_ID === run.workflowId)?.WORKFLOW_NAME || run.workflowName,
        initialAction: kind === 'edit' ? 'edit' : '', returnContext })
      return
    }
    const context = { workflowId: run.workflowId, workflowName: run.name ? `${run.workflowName} / ${run.name}` : run.workflowName, runId: run.runId, returnPage: 'orchestrationMonitor', returnContext }
    onNavigate(kind === 'history' ? 'orchestrationHistory' : kind === 'dag' ? 'dag' : 'executionLog', kind === 'history'
      ? { ...context, workflowName: run.workflowName, jobId: run.jobId || '', jobName: run.name || '' }
      : { ...context, orchestration: { runId: run.runId, jobId: kind === 'dag' && run.type !== 'DBT' ? '' : run.jobId || '', historical: !activeTimelineStatuses.has(run.status) } })
  }
  async function copyId(value, label) {
    try { await navigator.clipboard.writeText(value); setCopyNotice(`${label} copied.`) }
    catch { setCopyNotice('Copy unavailable. Select the ID above to copy it.') }
  }
  function tickLabel(at) {
    const date = new Date(at)
    const time = axisTime.format(date)
    return step < 1000 ? `${time}.${String(at % 1000).padStart(3, '0')}` : span >= 86400000 ? `${axisDate.format(date)} ${time.slice(0, 5)}` : time
  }

  return <div className="ot-timeline">
    <div className="ot-controls" role="group" aria-label="Timeline controls">
      <label>Range<select aria-label="Timeline range" value={ranges.some(([value]) => value === span) ? span : 'custom'} onChange={event => {
        if (event.target.value === 'custom') { setCustomStart(stockholmInput(start)); setCustomEnd(stockholmInput(end)); setRangeError(''); setCustom(true) }
        else setSpan(Number(event.target.value))
      }}>{ranges.map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="custom">Custom / zoomed</option></select></label>
      <div className="ot-button-group"><button className="button" type="button" aria-label="Previous time window" title="Previous time window" onClick={() => move(-1)}>←</button><button className="button" type="button" aria-label="Next time window" title="Next time window" onClick={() => move(1)}>→</button></div>
      <div className="ot-button-group"><button className="button" type="button" aria-label="Zoom in timeline" title="Zoom in" disabled={visibleSpan <= minZoom} onClick={() => zoom(.5)}>+</button><button className="button" type="button" aria-label="Zoom out timeline" title="Zoom out" disabled={visibleSpan >= span} onClick={() => zoom(2)}>−</button></div>
      <button className="button" type="button" disabled={!visibleRuns.length} onClick={fit}>Fit runs</button>
      <button className="button ot-relations-toggle" type="button" aria-pressed={showRelations} onClick={() => setShowRelations(value => !value)}>Relations</button>
      <button className="button" type="button" onClick={() => { setCustomStart(stockholmInput(start)); setCustomEnd(stockholmInput(end)); setRangeError(''); setCustom(!custom) }} aria-expanded={custom}>Custom range</button>
      <button className={`button ot-live ${live ? 'is-live' : ''}`} type="button" aria-pressed={live} onClick={() => { setAnchor(now); setLive(!live) }}><span aria-hidden="true">●</span> Live</button>
      <label className="ot-detail">Zoom<select aria-label="Timeline zoom" value={visibleSpan === span ? 'fit' : zooms.some(([value]) => value === visibleSpan) ? visibleSpan : 'custom'} onChange={event => setZoomSpan(event.target.value === 'fit' ? span : Number(event.target.value))}>{zooms.map(([value, label]) => <option value={value} key={value} disabled={value > span || value < minZoom}>{label}</option>)}<option value="fit">Fit range</option>{!zooms.some(([value]) => value === visibleSpan) && visibleSpan !== span && <option value="custom" disabled>{spanLabel(visibleSpan)} visible</option>}</select></label>
      <label>Rows<select aria-label="Timeline row density" value={compact ? 'compact' : 'comfortable'} onChange={event => setCompact(event.target.value === 'compact')}><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
    </div>
    {custom && <form className="ot-custom" onSubmit={applyRange}>
      <label>From · Europe/Stockholm<input type="datetime-local" step="1" required value={customStart} onChange={event => setCustomStart(event.target.value)} /></label>
      <label>To · Europe/Stockholm<input type="datetime-local" step="1" required value={customEnd} onChange={event => setCustomEnd(event.target.value)} /></label>
      <button className="button primary" type="submit">Apply range</button><span>Up to 30 days. Repeated autumn times use the first occurrence.</span>
      {rangeError && <div className="alert error" role="alert">{rangeError}</div>}
    </form>}
    <div className="ot-window"><span>{formatDateTimeSeconds(start)} → {formatDateTimeSeconds(end)} <span className="ot-zone">Europe/Stockholm</span></span><span>{spanLabel(visibleSpan)} visible · {spanLabel(span)} total</span><span role="status">{loading ? 'Loading execution history…' : `${visibleRuns.length} displayed / ${result?.runs.length || 0} history runs loaded`}</span></div>
    {error && <div className="alert error" role="alert">Timeline history: {error} <button className="button" type="button" onClick={() => setRetry(value => value + 1)}>Retry</button></div>}
    {result?.nextCursor && <div className="ot-more"><span>More executions are available in this window. Loaded runs may be incomplete for your filters.</span><button type="button" className="button" disabled={loading || loadingMore} onClick={loadMore}>{loadingMore ? 'Loading…' : 'Load more runs'}</button></div>}
    <input className="ot-scrollbar" type="range" aria-label="Horizontal timeline scrollbar" title="Drag to scroll through the selected range" min="0" max={Math.max(0, trackWidth - visibleWidth)} step="1" value={Math.min(scrollPosition, Math.max(0, trackWidth - visibleWidth))} disabled={trackWidth <= visibleWidth} style={{ '--ot-thumb-width': `${Math.max(24, viewportWidth * visibleSpan / span)}px` }} onChange={event => { viewport.current.scrollLeft = Number(event.target.value); setScrollPosition(viewport.current.scrollLeft) }} />
    <div className={`ot-viewport ${compact ? 'ot-compact' : ''}`} ref={viewport} onScroll={syncScroll} tabIndex={0} role="region" aria-label="Execution timeline. Scroll horizontally through the selected range." style={{ '--ot-label-width': `${labelWidth}px`, '--ot-track-width': `${trackWidth}px` }}>
      <div className="ot-canvas">
        <div className="ot-row ot-head"><div className="ot-label">Workflow / Job</div><div className="ot-axis">{ticks.map(tick => <span key={tick.at} style={{ left: `${tick.left}%` }}>{tickLabel(tick.at)}</span>)}</div></div>
        <div className="ot-body">
        {layout.rows.map(row => {
          const { lanes } = row
          return <div className={`ot-row ${row.job ? 'ot-job' : 'ot-workflow'}`} key={row.key} style={{ height: `${row.height}px` }}>
            <div className="ot-label">
              {!row.job && <HierarchyToggle expanded={expanded.has(row.workflow.WORKFLOW_ID)} onClick={() => onToggle(row.workflow.WORKFLOW_ID)} aria-label={`${expanded.has(row.workflow.WORKFLOW_ID) ? 'Hide' : 'Show'} jobs for ${row.workflow.WORKFLOW_NAME}`} />}
              {row.job && <span className="ot-job-order">{row.job.position || '↳'}</span>}
              <div className="ot-label-text"><button type="button" className="ot-name" title={row.job?.name || row.workflow.WORKFLOW_NAME} onClick={() => onNavigate('orchestrationHistory', { workflowId: row.workflow.WORKFLOW_ID, workflowName: row.workflow.WORKFLOW_NAME, jobId: row.job?.id || '', jobName: row.job?.name || '' })}>{row.job?.name || row.workflow.WORKFLOW_NAME}</button>
                <small>{row.job ? `${row.job.type || 'Job'}${row.job.historical ? ' · From execution snapshot' : ''}` : [row.workflow.WORKFLOW_GROUP || 'Ungrouped', row.workflow.parentWorkflowName && `After ${row.workflow.parentWorkflowName}`, row.workflow.IS_SCHEDULE_ENABLED ? `${row.workflow.SCHEDULE} · ${row.workflow.SCHEDULE_TIMEZONE || 'UTC'}` : 'Manual trigger'].filter(Boolean).join(' · ')}</small></div>
              <span className="ot-count" title="Executions in this row">{lanes.items.length}</span>
            </div>
            <div className="ot-track">
              {ticks.map(tick => <i className="ot-gridline" key={tick.at} style={{ left: `${tick.left}%` }} />)}
              {now >= start && now <= end && <i className="ot-now" style={{ left: `${Math.min(99.98, (now - start) / span * 100)}%` }} />}
              {lanes.items.map(({ run, geometry, lane }) => {
                const label = `${row.job?.name || row.workflow.WORKFLOW_NAME} · ${run.status} · ${preciseDuration(run, now)}`
                return <button type="button" key={run.executionId || run.runId} data-execution-id={run.executionId} aria-haspopup="dialog" className={`ot-bar ${statusKind(run.status === 'CLAIMED' ? 'INITIATING' : run.status)} ${!run.start ? 'ot-waiting' : ''} ${geometry.clippedStart ? 'ot-clipped-start' : ''} ${geometry.clippedEnd ? 'ot-clipped-end' : ''} ${selection?.executionId === run.executionId && selection?.workflowId === run.workflowId ? 'ot-selected' : ''} ${showRelations && selection ? connected.has(executionKey(run)) ? 'ot-related' : 'ot-unrelated' : ''}`} style={{ left: `min(${geometry.left}%, calc(100% - 5px))`, width: `max(5px, ${geometry.width}%)`, maxWidth: `max(5px, ${100 - geometry.left}%)`, top: `${6 + lane * (compact ? 23 : 28)}px` }} aria-label={`${label}. Requested ${exact(run.requestedAt)}. Started ${exact(run.start)}. Open run actions.`} title={`${label}\nRequested: ${exact(run.requestedAt)}\nStart: ${exact(run.start)}\nEnd: ${exact(run.end)}\nClick for run actions`} onClick={() => { setSelection(run); setCopyNotice(''); setActionsOpen(true) }}><span>{run.status} · {preciseDuration(run, now)}</span></button>
              })}
              {!lanes.items.length && <span className="ot-empty">{loading ? 'Loading…' : 'No executions in this window'}</span>}
            </div>
          </div>
        })}
        {showRelations && layout.connections.length > 0 && <svg className="ot-connections" width={trackWidth} height={layout.height} viewBox={`0 0 ${trackWidth} ${layout.height}`} role="img" aria-label={`${layout.connections.length} execution relationships`}>
          <defs>{['normal', 'highlight'].map(kind => <marker key={kind} id={`${markerId}-${kind}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M 0 0 L 8 4 L 0 8 z" fill={kind === 'highlight' ? '#e5f3ff' : '#9abde9'} /></marker>)}</defs>
          {layout.connections.map(connection => {
            const highlighted = selection && connected.has(executionKey(connection.child))
            return <g key={connection.id} className={`ot-connection ${connection.waiting ? 'ot-connection-waiting' : ''} ${highlighted ? 'ot-connection-highlight' : selection ? 'ot-connection-muted' : ''}`} data-parent-execution={connection.parent.executionId} data-child-execution={connection.child.executionId}>
              <title>{connection.parent.name || connection.parent.workflowName} → {connection.child.name || connection.child.workflowName}{connection.waiting ? ' · Waiting for parent' : ''}</title>
              <path className="ot-connection-halo" d={connection.path} />
              <path className="ot-connection-line" d={connection.path} markerEnd={`url(#${markerId}-${highlighted ? 'highlight' : 'normal'})`} />
            </g>
          })}
        </svg>}
        </div>
        {!rows.length && <div className="ot-no-workflows">No workflows match your filters.</div>}
      </div>
    </div>
    <div className="ot-legend">{['SUCCESS', 'FAILED', 'RUNNING', 'WARNING', 'QUEUED', 'SKIPPED', 'CANCELLED'].map(status => <span key={status}><i className={statusKind(status)} />{status.toLowerCase()}</span>)}{showRelations && <span className="ot-relation-key">↳ Arrows show execution relationships{layout.connections.length ? ' · Select a run to trace its chain' : ' · Expand workflows to see their job links'}</span>}<span className="ot-legend-note">Dashed = waiting · Short events have a 5 px target</span></div>
    {selected && actionsOpen && <EditorDialog title="Run actions" onClose={() => setActionsOpen(false)}><div className="vision-modal ot-selection ot-run-dialog" role="region" aria-label="Selected execution details">
      <div className="modal-header"><div><h2>Run actions</h2><p>{selected.workflowName}{selected.name ? ` / ${selected.name}` : ''}</p></div><button className="modal-close" type="button" aria-label="Close run actions" onClick={() => setActionsOpen(false)}>×</button></div>
      <div className="ot-selection-title"><StatusBadge status={selected.status} /><span>{selected.jobId ? `${selected.type || 'Job'} execution` : 'Workflow execution'} · {preciseDuration(selected, now)}</span></div>
      <div className="ot-run-actions">
        <button type="button" className="button" onClick={() => navigate(selected, 'log')}><ActionIcon name="log" />View log</button>
        <button type="button" className="button" disabled={selected.type === 'DBT' && !selected.start} title={selected.type === 'DBT' && !selected.start ? 'This dbt job has not started yet.' : 'Open the DAG for this execution'} onClick={() => navigate(selected, 'dag')}><ActionIcon name="dag" />{selected.jobId && selected.type !== 'DBT' ? 'Show workflow DAG run' : 'Show DAG run'}</button>
        <button type="button" className="button" onClick={() => navigate(selected, 'history')}><ActionIcon name="history" />Execution history</button>
        {selected.type === 'DBT' && <button type="button" className="button" disabled={!selectedJob} title="Preview the current saved job definition" onClick={() => { setPreview({ id: selected.jobId, name: selectedJob.JOB_NAME }); setActionsOpen(false) }}><ActionIcon name="dag" />Preview DAG</button>}
        <button type="button" className="button" onClick={() => { setActionsOpen(false); focusRun(selected) }}>Zoom to execution</button>
        <button type="button" className="button" onClick={() => { setShowRelations(true); setActionsOpen(false) }}>Trace relations</button>
      </div>
      <dl><div><dt>Requested</dt><dd>{exact(selected.requestedAt)}</dd></div><div><dt>Started</dt><dd>{exact(selected.start)}</dd></div><div><dt>Ended</dt><dd>{selected.end ? exact(selected.end) : ['RUNNING', 'CLAIMED', 'QUEUED', 'PENDING', 'INITIATING'].includes(selected.status) ? 'In progress' : '—'}</dd></div><div><dt>Duration</dt><dd>{preciseDuration(selected, now)}</dd></div><div><dt>Requested by</dt><dd>{selected.requestedBy || '—'}</dd></div><div><dt>Run ID</dt><dd>{selected.runId}</dd></div></dl>
      {selected.jobId && <div className="ot-execution-id"><span>Job execution ID</span><code>{selected.executionId}</code></div>}
      {selected.message && <p className="ot-message">{selected.message}</p>}
      <div className="ot-selection-actions"><button type="button" className="button" onClick={() => copyId(selected.runId, 'Run ID')}><ActionIcon name="copy" />Copy run ID</button>{selected.jobId && <button type="button" className="button" onClick={() => copyId(selected.executionId, 'Execution ID')}><ActionIcon name="copy" />Copy execution ID</button>}</div>
      {copyNotice && <p role="status" className="ot-copy-notice">{copyNotice}</p>}
      <p className="ot-run-help">Current workflow configuration</p>
      <div className="ot-selection-actions"><button type="button" className="button" onClick={() => navigate(selected, 'edit')}><ActionIcon name="edit" />Edit workflow</button><button type="button" className="button" onClick={() => navigate(selected, 'dependencies')}><ActionIcon name="dag" />Dependencies</button><button type="button" className="button" onClick={() => onWorkflowControls(selected.workflowId)}>Workflow controls</button></div>
      <p className="ot-run-help">Workflow controls include run, run from job, enable/disable and schedule settings. Preview DAG uses the current saved job definition. Times in Europe/Stockholm.</p>
    </div></EditorDialog>}
    {preview && <DagPreviewDialog definitionId={preview.id} name={preview.name} orchestration onClose={() => { setPreview(null); setActionsOpen(true) }} />}
  </div>
}
