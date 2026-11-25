import { useState } from 'react'
import { api } from '../api.js'
import EditorDialog from '../components/EditorDialog.jsx'
import { activeWorkflowStatuses } from '../utils/useOrchestrationMonitor.js'

export function RunWorkflowDialog({ workflow, jobId, jobs, onClose, onAccepted, initialMode }) {
  const [mode, setMode] = useState(initialMode || (jobId ? 'JOB_ONLY' : 'WORKFLOW'))
  const [fullRefresh, setFullRefresh] = useState(false)
  const [requestId] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [queued, setQueued] = useState(null)
  const [submitted, setSubmitted] = useState(false)
  const [includeChildren, setIncludeChildren] = useState(workflow.canRunWithChildren !== false)
  const scopeUnavailable = workflow.source === 'POSTGRES' && includeChildren && workflow.canRunWithChildren === false
  const unavailable = workflow.canRun === false || activeWorkflowStatuses.has(workflow.run?.status) || scopeUnavailable
  const index = jobs.findIndex(job => job.JOB_ID === jobId)
  const dbtJob = jobId && jobs[index]?.JOB_TYPE === 'DBT'
  const scope = mode === 'WORKFLOW' ? jobs : mode === 'JOB_ONLY' ? jobs.slice(index, index + 1) : jobs.slice(index)
  const selected = scope.filter(job => (job.JOB_IS_ENABLED ?? job.IS_ENABLED) !== false)
  const parallel = workflow.JOB_EXECUTION_MODE === 'PARALLEL'
  async function run(event) {
    event.preventDefault()
    if (busy || unavailable) return
    setBusy(true); setSubmitted(true); setError('')
    try {
      const result = workflow.source === 'POSTGRES'
        ? await api.startTaskPrototypeGraph(workflow.graphId, { workflowId: workflow.WORKFLOW_ID, requestId, includeChildren })
        : await api.runOrchestrationWorkflow(workflow.WORKFLOW_ID, { mode, jobId: jobId || null, requestId, fullRefresh: Boolean(dbtJob && fullRefresh) })
      if (result.warning) { setQueued(result); setError(result.warning) }
      else onAccepted(result)
    } catch (failure) { setError(failure.message) }
    finally { setBusy(false) }
  }
  return <EditorDialog title={`Run ${workflow.WORKFLOW_NAME}`} busy={busy} onClose={onClose}>
    <form className="vision-modal edit-form" onSubmit={run}>
      <div className="modal-header"><div><h2>{workflow.WORKFLOW_NAME}</h2><p>Choose which jobs to execute.</p></div><button className="modal-close" type="button" aria-label="Close dialog" disabled={busy} onClick={onClose}>×</button></div>
      {error && <div className="alert error" role="alert">{error}</div>}
      {unavailable && <div className="alert info" role="status">{workflow.runUnavailable || (scopeUnavailable && workflow.runWithChildrenUnavailable) || 'Workflow is already queued or running.'}</div>}
      {jobId ? <div className="form-field"><label htmlFor="workflow-run-mode">Run mode</label><select id="workflow-run-mode" disabled={busy || submitted} value={mode} onChange={event => setMode(event.target.value)}><option value="JOB_ONLY">Run only this job</option><option value="FROM_JOB">Run from this job</option></select></div> : <p>{parallel ? 'Run all enabled jobs in parallel.' : 'Run all enabled jobs in workflow order.'}</p>}
      {dbtJob && <label className={`dbt-full-refresh ${fullRefresh ? 'active' : ''}`}>
        <input type="checkbox" checked={fullRefresh} disabled={busy || submitted} onChange={event => setFullRefresh(event.target.checked)} />
        <span><strong>Full refresh</strong><small>Rebuild incremental models for {jobs[index].JOB_NAME} in this run. Supported for dbt build, run and seed. Later jobs use their saved commands.</small></span>
      </label>}
      {workflow.source === 'POSTGRES' && <>
        <div className="form-field"><label htmlFor="postgres-run-scope">Execution scope</label><select id="postgres-run-scope" value={includeChildren ? 'DEPENDENCIES' : 'WORKFLOW'} disabled={busy || submitted} onChange={event => setIncludeChildren(event.target.value === 'DEPENDENCIES')}>
          <option value="WORKFLOW">Run only this workflow</option><option value="DEPENDENCIES">Run workflow and dependencies</option>
        </select></div>
        <div className="alert info"><strong>{includeChildren ? 'Start here and evaluate downstream workflows' : 'Run only this workflow'}</strong><p>{includeChildren ? workflow.runScope?.join(', ') : workflow.WORKFLOW_NAME}</p><p>Ancestors are not rerun. This workflow and its direct parents and children must be idle before starting. The same one-level overlap rule applies to each downstream step. Saved parent rules still apply. Disabled workflows or jobs are skipped.</p></div>
      </>}
      <ol className="wa-run-jobs">{selected.map(job => <li key={job.JOB_ID}><strong>{job.JOB_NAME}</strong><span>{job.JOB_TYPE} · {job.JOB_LAYER || 'GENERAL'}</span></li>)}</ol>
      <p className="form-help">{mode !== 'WORKFLOW' ? 'Earlier jobs are not run. ' : ''}{parallel && selected.length > 1 ? 'Selected jobs start together and run independently. ' : ''}Disabled jobs are omitted. Job conditions apply. {mode === 'WORKFLOW' && (workflow.source !== 'POSTGRES' || includeChildren) ? 'Downstream workflows are evaluated after completion.' : 'This run does not start downstream workflows.'}</p>
      <div className="modal-actions"><button className="button" type="button" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={busy || unavailable || !selected.length}>{busy ? 'Submitting…' : queued ? 'Retry start' : mode === 'JOB_ONLY' ? 'Run job' : mode === 'FROM_JOB' ? 'Run from job' : 'Run workflow'}</button></div>
    </form>
  </EditorDialog>
}
