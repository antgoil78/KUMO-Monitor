import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import ActionIcon from '../components/ActionIcon.jsx'
import HierarchyToggle from '../components/HierarchyToggle.jsx'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import useOrchestrationMonitor from '../utils/useOrchestrationMonitor.js'
import { elapsedDuration, formatDateTimeSeconds } from '../utils/time.js'
import './OrchestrationMonitor.css'
import './OrchestrationHistory.css'

const statuses = ['RUNNING', 'QUEUED', 'CLAIMED', 'SUCCESS', 'WARNING', 'FAILED', 'SKIPPED', 'CANCELLED']

function liveRun(run, live, loadedAt) {
  if (Date.parse(live?.generatedAt || '') < Date.parse(loadedAt || '')) return run
  const workflow = live?.rows?.find(row => row.WORKFLOW_ID === run.workflowId && row.run.runId === run.runId)
  if (!workflow) return run
  return { ...run, ...workflow.run, jobs: run.jobs.map(job => {
    const current = workflow.jobs.find(row => row.JOB_ID === job.jobId && row.run.executionId === job.executionId)
    return current ? { ...job, ...current.run } : job
  }) }
}

function RowStatus({ status }) {
  return <span className="om-live-status">{['RUNNING', 'QUEUED', 'CLAIMED', 'INITIATING'].includes(status) && <span className="om-spinner" aria-label="Execution in progress" role="img" />}<StatusBadge status={status === 'CLAIMED' ? 'INITIATING' : status} /></span>
}

export default function OrchestrationHistory({ workflowId = '', workflowName = '', jobId = '', jobName = '', historyState = null, onNavigate, returnContext = null }) {
  const [filters, setFilters] = useState(() => historyState?.filters || { workflowId, jobId, search: '', status: '', days: 30, limit: 25 })
  const [search, setSearch] = useState(filters.search)
  const [cursors, setCursors] = useState(historyState?.cursors || [''])
  const [page, setPage] = useState(historyState?.page || 0)
  const [expanded, setExpanded] = useState(() => new Set(historyState?.expanded || []))
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const [now, setNow] = useState(Date.now())
  const [copied, setCopied] = useState('')
  const force = useRef(false)
  const { data: live } = useOrchestrationMonitor()
  const query = useMemo(() => ({ ...filters, cursor: cursors[page] || '' }), [filters, cursors, page])

  useEffect(() => {
    let active = true
    setLoading(true); setError(''); setResult(null)
    api.orchestrationHistory(query, force.current)
      .then(value => { if (active) setResult(value) })
      .catch(failure => { if (active) setError(failure.message) })
      .finally(() => { if (active) setLoading(false) })
    force.current = false
    return () => { active = false }
  }, [query, reload])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const runs = (result?.runs || []).map(run => liveRun(run, live, result.generatedAt))
  const workflowOptions = new Map((live?.rows || []).map(row => [row.WORKFLOW_ID, row.WORKFLOW_NAME]))
  const jobOptions = new Map()
  for (const row of live?.rows || []) if (!filters.workflowId || row.WORKFLOW_ID === filters.workflowId) {
    for (const job of row.jobs) jobOptions.set(job.JOB_ID, job.JOB_NAME)
  }
  for (const run of runs) {
    workflowOptions.set(run.workflowId, run.workflowName)
    for (const job of run.jobs) jobOptions.set(job.jobId, job.name)
  }
  if (workflowId) workflowOptions.set(workflowId, workflowName || workflowId)
  if (jobId) jobOptions.set(jobId, jobName || jobId)
  if (filters.workflowId && !workflowOptions.has(filters.workflowId)) workflowOptions.set(filters.workflowId, filters.workflowId)
  if (filters.jobId && !jobOptions.has(filters.jobId)) jobOptions.set(filters.jobId, filters.jobId)

  function change(patch) {
    setFilters(previous => ({ ...previous, ...patch })); setCursors(['']); setPage(0); setExpanded(new Set())
  }
  function toggle(id) {
    setExpanded(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next })
  }
  function open(run, job, kind) {
    const historyContext = { workflowId, workflowName, jobId, jobName, returnContext,
      historyState: { filters, cursors, page, expanded: [...expanded] } }
    onNavigate(kind === 'log' ? 'executionLog' : 'dag', { workflowId: run.workflowId,
      workflowName: job ? `${run.workflowName} / ${job.name}` : run.workflowName,
      workflowType: job?.type || (run.jobs.some(row => row.type === 'DBT') ? 'DBT' : 'SQL'),
      runId: run.runId, returnPage: 'orchestrationHistory', returnContext: historyContext,
      orchestration: { runId: run.runId, jobId: job?.jobId || '', historical: true } })
  }
  async function copy(id) {
    try { await navigator.clipboard.writeText(id); setCopied(id) }
    catch { setError('Could not copy the execution ID. It is available in the row tooltip and execution log.') }
  }
  function shortcuts(run, job) {
    const id = job?.executionId || run.runId
    const name = job?.name || run.workflowName
    return <div className="oh-actions">
      {(!job || job.type === 'DBT') && <button type="button" className="om-shortcut" disabled={Boolean(job && !job.start)} title={job && !job.start ? 'No started job execution' : 'DAG Run'} aria-label={`DAG Run for ${name}`} onClick={() => open(run, job, 'dag')}><ActionIcon name="dag" /></button>}
      <button type="button" className="om-shortcut" title="Execution log" aria-label={`Log for ${name}`} onClick={() => open(run, job, 'log')}><ActionIcon name="log" /></button>
      <button type="button" className="om-shortcut" title={copied === id ? 'Copied' : 'Copy execution ID'} aria-label={`Copy execution ID for ${name}`} onClick={() => copy(id)}><ActionIcon name="copy" /></button>
    </div>
  }
  const sorted = options => [...options].sort((a, b) => a[1].localeCompare(b[1]))

  return <section className="page om-page oh-page">
    <PageHeader breadcrumb="Workflow / History" title="Execution History" subtitle="Workflow runs and their jobs, in execution order." actions={<div className="oh-header-actions"><button className="button" type="button" onClick={() => onNavigate('orchestrationMonitor', returnContext || {})}>← Monitor</button><button className="button" type="button" disabled={loading} onClick={() => { force.current = true; setReload(value => value + 1) }}>{loading ? 'Loading…' : '↻ Refresh'}</button></div>} />
    {error && <div className="alert error" role="alert">{error}</div>}
    <form className="oh-filters" onSubmit={event => { event.preventDefault(); change({ search: search.trim() }) }}>
      <div className="oh-search"><input className="search-input" type="search" aria-label="Search execution history" placeholder="Workflow, job, requester or run ID…" value={search} onChange={event => setSearch(event.target.value)} /><button className="button" type="submit">Search</button></div>
      <label>Workflow<select className="status-select" aria-label="Filter history workflow" value={filters.workflowId} onChange={event => change({ workflowId: event.target.value, jobId: '' })}><option value="">All workflows</option>{sorted(workflowOptions).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>Job<select className="status-select" aria-label="Filter history job" value={filters.jobId} onChange={event => change({ jobId: event.target.value })}><option value="">All jobs</option>{sorted(jobOptions).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>Status<select className="status-select" aria-label="Filter history status" value={filters.status} onChange={event => change({ status: event.target.value })}><option value="">All statuses</option>{statuses.map(status => <option key={status}>{status}</option>)}</select></label>
      <label>Period<select className="status-select" aria-label="Filter history period" value={filters.days} onChange={event => change({ days: Number(event.target.value) })}>{[[1, 'Last 24 hours'], [7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 90 days'], [0, 'All time']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </form>
    <div className="oh-list-heading"><span>{runs.length} workflow {runs.length === 1 ? 'run' : 'runs'}{filters.jobId && ` · ${jobOptions.get(filters.jobId) || 'Selected job'}`}{result && ` · Loaded ${formatDateTimeSeconds(result.generatedAt)}`}</span><div><button type="button" className="button" disabled={!runs.length} onClick={() => setExpanded(new Set(runs.map(run => run.runId)))}>Expand all</button><button type="button" className="button" onClick={() => setExpanded(new Set())}>Collapse all</button></div></div>
    {loading && <LoadingState>Loading workflow executions…</LoadingState>}
    {!loading && result && <div className="om-table-wrap"><table className="om-table oh-table">
      <caption className="visually-hidden">Workflow execution history with expandable jobs. Times are in Europe/Stockholm. Log and DAG links open the selected execution.</caption>
      <colgroup><col className="oh-name-col" /><col className="oh-status-col" /><col className="oh-mode-col" /><col /><col /><col className="oh-duration-col" /><col className="oh-requester-col" /><col className="oh-actions-col" /></colgroup>
      <thead><tr>{['Workflow / Job', 'Status', 'Mode / Trigger', 'Requested', 'Started', 'Duration', 'Requested by', 'Details'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
      {runs.map(run => <Fragment key={run.runId}>
        <tbody className="om-parent"><tr>
          <th scope="row"><div className="om-name"><HierarchyToggle expanded={expanded.has(run.runId)} onClick={() => toggle(run.runId)} aria-controls={`history-${run.runId}`} aria-label={`${expanded.has(run.runId) ? 'Hide' : 'Show'} jobs for run ${run.runId}`} /><div className="oh-name"><strong title={`${run.workflowName}\n${run.runId}`}>{run.workflowName}</strong><small title={run.message || run.runId} className={run.message ? 'oh-error' : ''}>{run.message || `${run.jobs.length} jobs · ${run.workflowGroup || 'Ungrouped'} · ${run.runId.slice(0, 8)}`}</small></div></div></th>
          <td><RowStatus status={run.status} /></td><td>{run.mode?.replaceAll('_', ' ') || '—'}<small>{run.triggerSource || '—'}</small></td>
          <td className="om-time">{formatDateTimeSeconds(run.requestedAt)}</td><td className="om-time">{formatDateTimeSeconds(run.start)}</td><td className="om-time">{elapsedDuration(run.start, run.end, run.status, now)}</td><td className="oh-requester" title={run.requestedBy}>{run.requestedBy || '—'}</td><td>{shortcuts(run)}</td>
        </tr></tbody>
        <tbody id={`history-${run.runId}`} className="om-children" hidden={!expanded.has(run.runId)}>
          {run.jobs.filter(job => !filters.jobId || job.jobId === filters.jobId).map(job => <tr key={job.executionId}>
            <th scope="row"><div className="om-job-name"><span className="om-sequence">{String(job.position || '—').padStart(2, '0')}</span><div className="oh-name"><strong title={`${job.name}\n${job.executionId}`}>{job.name}</strong><small className={job.status === 'FAILED' ? 'oh-error' : ''} title={job.message || `${job.type} · ${job.layer || 'GENERAL'}`}>{job.message || `${job.type} · ${job.layer || 'GENERAL'}`}</small></div></div></th>
            <td><RowStatus status={job.status} /></td><td><span className="om-kind">{job.type || '—'}</span><small>{job.layer || 'GENERAL'}</small></td><td className="om-time">{formatDateTimeSeconds(job.requestedAt)}</td><td className="om-time">{formatDateTimeSeconds(job.start)}</td><td className="om-time">{elapsedDuration(job.start, job.end, job.status, now)}</td><td className="oh-requester" title={job.requestedBy}>{job.requestedBy || run.requestedBy || '—'}</td><td>{shortcuts(run, job)}</td>
          </tr>)}
          {!run.jobs.length && <tr><td colSpan={8} className="om-empty-jobs">No job execution records for this run.</td></tr>}
        </tbody>
      </Fragment>)}
      {!runs.length && <tbody><tr><td colSpan={8} className="om-empty-jobs">No executions match these filters.</td></tr></tbody>}
    </table></div>}
    <div className="oh-pagination"><span>Page {page + 1} · Europe/Stockholm</span><div><button className="button" type="button" disabled={loading || page === 0} onClick={() => setPage(value => value - 1)}>← Newer</button><button className="button" type="button" disabled={loading || !result?.nextCursor} onClick={() => { setCursors(previous => [...previous.slice(0, page + 1), result.nextCursor]); setPage(value => value + 1) }}>Older →</button></div></div>
    <p className="om-footnote">Names and job order come from each execution’s saved configuration. Visible live statuses use shared SSE updates. Refresh to load new runs. Log and DAG Run always open the selected run; SQL jobs have no model DAG.</p>
  </section>
}
