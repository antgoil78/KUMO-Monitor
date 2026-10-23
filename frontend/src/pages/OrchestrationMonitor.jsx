import { useMemo, useState } from 'react'
import PageHeader from '../components/PageHeader.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { assignments, jobRuns, jobs, sampleSnapshot, workflowRuns, workflows } from './orchestrationMonitorSample.js'
import './OrchestrationMonitor.css'

const jobById = new Map(jobs.map(job => [job.JOB_ID, job]))
const rows = workflows.map(workflow => ({
  ...workflow,
  run: workflowRuns[workflow.WORKFLOW_ID],
  jobs: assignments.filter(item => item.WORKFLOW_ID === workflow.WORKFLOW_ID)
    .sort((left, right) => left.JOB_SEQUENCE - right.JOB_SEQUENCE)
    .map(item => ({ ...jobById.get(item.JOB_ID), ...item, run: jobRuns[item.WORKFLOW_JOB_ID] }))
}))
const statuses = ['RUNNING', 'FAILED', 'SUCCESS', 'QUEUED', 'NOT RUN']
const timeFormatter = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit', second: '2-digit' })

function startLabel(run) {
  return run.start ? timeFormatter.format(new Date(run.start)) : '—'
}

function durationLabel(run) {
  if (!run.start) return '—'
  const seconds = Math.floor((new Date(run.end || sampleSnapshot) - new Date(run.start)) / 1000)
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`
}

function matches(workflow, query) {
  return [workflow.WORKFLOW_NAME, workflow.WORKFLOW_GROUP, ...workflow.jobs.map(job => job.JOB_NAME)]
    .some(value => value.toLowerCase().includes(query))
}

function RunCells({ run }) {
  return <><td><StatusBadge status={run.status} /></td><td className="om-time">{startLabel(run)}</td><td className="om-time">{durationLabel(run)}</td></>
}

function WorkflowRows({ workflow, expanded, onToggle }) {
  const completed = workflow.jobs.filter(job => job.run.status === 'SUCCESS').length
  const failed = workflow.jobs.filter(job => job.run.status === 'FAILED').length
  const percent = workflow.jobs.length ? Math.round(completed / workflow.jobs.length * 100) : 0
  const bodyId = `om-jobs-${workflow.WORKFLOW_ID}`
  return <>
    <tbody className="om-parent">
      <tr>
        <th scope="row">
          <div className="om-name">
            <button className="om-toggle" type="button" onClick={onToggle} aria-expanded={expanded} aria-controls={bodyId} aria-label={`${expanded ? 'Hide' : 'Show'} jobs for ${workflow.WORKFLOW_NAME}`}><span aria-hidden="true">{expanded ? '⌄' : '›'}</span></button>
            <div><strong>{workflow.WORKFLOW_NAME}</strong><small>{workflow.WORKFLOW_GROUP}{!workflow.IS_ENABLED && ' · Disabled'}</small></div>
          </div>
          {workflow.run.message && <p className="om-error">{workflow.run.message}</p>}
        </th>
        <td><span className="om-kind">Workflow</span><small>{workflow.jobs.length} jobs</small></td>
        <RunCells run={workflow.run} />
        <td>{workflow.jobs.length ? <div className={`om-progress ${failed ? 'has-failure' : ''}`}><span>{completed} / {workflow.jobs.length} succeeded{failed ? ` · ${failed} failed` : ''}</span><div className="om-track"><i style={{ width: `${percent}%` }} /></div></div> : <span className="om-muted">No jobs</span>}</td>
        <td>{workflow.SCHEDULE ? <><span>{workflow.SCHEDULE}</span><small>{workflow.SCHEDULE_TIMEZONE}</small></> : <span className="om-muted">{workflow.ALLOW_EXTERNAL_TRIGGER ? 'External trigger' : 'Manual trigger'}</span>}</td>
      </tr>
    </tbody>
    <tbody id={bodyId} className="om-children" hidden={!expanded}>
      {workflow.jobs.map(job => <tr key={job.WORKFLOW_JOB_ID}>
        <th scope="row"><div className="om-job-name"><span className="om-sequence" title="Job sequence">{String(job.JOB_SEQUENCE).padStart(2, '0')}</span><div><strong>{job.JOB_NAME}</strong>{job.run.message && <small className={job.run.status === 'FAILED' ? 'om-error' : ''}>{job.run.message}</small>}</div></div></th>
        <td><span className="om-kind">{job.JOB_TYPE}</span><small>{job.JOB_LAYER}</small></td>
        <RunCells run={job.run} />
        <td><span className="om-muted">{job.run.status === 'SUCCESS' ? 'Completed' : job.run.status === 'RUNNING' ? 'In progress' : job.run.status === 'FAILED' ? 'Failed' : job.run.status === 'SKIPPED' ? 'Skipped' : 'Awaiting execution'}</span></td>
        <td>{job.PARENT_JOB_ID ? <><span className="om-dependency">After {jobById.get(job.PARENT_JOB_ID).JOB_NAME}</span><small>Requires {job.MIN_STATUS_LEVEL.toLowerCase()}</small></> : <span className="om-muted">First job</span>}</td>
      </tr>)}
      {!workflow.jobs.length && <tr><td colSpan={7} className="om-empty-jobs">No jobs assigned. This workflow is an inactive placeholder.</td></tr>}
    </tbody>
  </>
}

export default function OrchestrationMonitor() {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [group, setGroup] = useState('')
  const [expanded, setExpanded] = useState(() => new Set(['wf-customer', 'wf-finance']))
  const visible = useMemo(() => rows.filter(workflow => matches(workflow, search.trim().toLowerCase()) && (!status || workflow.run.status === status) && (!group || workflow.WORKFLOW_GROUP === group)), [search, status, group])

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

  return <section className="page om-page">
    <PageHeader breadcrumb="Orchestration / Monitor" title="Orchestration Monitor" subtitle="Parent workflows and their underlying jobs, in execution order." actions={<span className="om-sketch-label">Visualization sketch</span>} />
    <div className="om-notice"><strong>Sample data</strong><span>Illustrative configuration and run states · Snapshot 23 Oct 2026, 08:15 Europe/Stockholm</span></div>
    <div className="monitor-toolbar om-toolbar">
      <input className="search-input" type="search" aria-label="Search workflows, groups or jobs" placeholder="Search workflows, groups or jobs…" value={search} onChange={event => changeSearch(event.target.value)} />
      <div className="monitor-toolbar-actions">
        <select className="status-select" aria-label="Filter workflow group" value={group} onChange={event => setGroup(event.target.value)}><option value="">All groups</option>{[...new Set(rows.map(workflow => workflow.WORKFLOW_GROUP))].sort().map(value => <option key={value}>{value}</option>)}</select>
        <select className="status-select" aria-label="Filter workflow status" value={status} onChange={event => setStatus(event.target.value)}><option value="">All workflow statuses</option>{statuses.map(value => <option key={value}>{value}</option>)}</select>
      </div>
    </div>
    <div className="om-summary" aria-label="Workflow summary">
      {statuses.slice(0, 4).map(value => <span key={value}><StatusBadge status={value} /><strong>{visible.filter(workflow => workflow.run.status === value).length}</strong></span>)}
      <span className="om-total" aria-live="polite">{visible.length} of {rows.length} workflows · {visible.reduce((total, workflow) => total + workflow.jobs.length, 0)} job assignments</span>
    </div>
    <div className="om-list-heading"><span>Workflow / Job hierarchy</span><div><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(current => new Set([...current, ...visible.map(workflow => workflow.WORKFLOW_ID)]))}>Expand all</button><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(new Set())}>Collapse all</button></div></div>
    <div className="om-table-wrap">
      <table className="om-table">
        <caption className="visually-hidden">Sample workflows with expandable jobs. Run times are in Europe/Stockholm. Status and group filters apply to workflows.</caption>
        <colgroup><col className="om-name-col" /><col /><col /><col /><col /><col /><col className="om-trigger-col" /></colgroup>
        <thead><tr>{['Workflow / Job', 'Type / Layer', 'Run status', 'Last start', 'Duration', 'Job progress', 'Schedule / Dependency'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
        {visible.map(workflow => <WorkflowRows key={workflow.WORKFLOW_ID} workflow={workflow} expanded={expanded.has(workflow.WORKFLOW_ID)} onToggle={() => toggle(workflow.WORKFLOW_ID)} />)}
        {!visible.length && <tbody><tr><td colSpan={7} className="om-empty-jobs">No workflows match your filters.</td></tr></tbody>}
      </table>
    </div>
    <p className="om-footnote">Jobs follow their configured execution order. Reusable jobs appear under each assigned workflow with their own run state. Progress counts successful jobs.</p>
  </section>
}
