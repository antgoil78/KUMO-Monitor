import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import EditorDialog from '../components/EditorDialog.jsx'
import DagPreviewDialog from '../components/DagPreviewDialog.jsx'
import HierarchyToggle from '../components/HierarchyToggle.jsx'
import useOrchestrationMonitor, { activeWorkflowStatuses, applySubmission } from '../utils/useOrchestrationMonitor.js'
import { JobEditor, WorkflowEditor } from './OrchestrationCatalog.jsx'
import { RunWorkflowDialog } from './WorkflowExecution.jsx'
import { moveJob, workflowJobIds } from '../utils/workflowOrder.js'
import './OrchestrationMonitor.css'
import './WorkflowAdministration.css'

function State({ enabled }) {
  return <span className={`orchestration-state ${enabled ? 'enabled' : 'disabled'}`}><i />{enabled ? 'Enabled' : 'Disabled'}</span>
}

function AssignJob({ workflow, jobs, assigned, busy, onClose, onAdd, onCreate, canCreate }) {
  const [jobId, setJobId] = useState('')
  const available = jobs.filter(job => !assigned.includes(job.JOB_ID))
  return <EditorDialog title={`Add job to ${workflow.WORKFLOW_NAME}`} busy={busy} onClose={onClose}>
    <form className="vision-modal edit-form" onSubmit={event => { event.preventDefault(); if (jobId) onAdd(jobId) }}>
      <div className="modal-header"><div><h2>Add job</h2><p>{workflow.WORKFLOW_NAME}</p></div><button className="modal-close" type="button" aria-label="Close dialog" onClick={onClose}>×</button></div>
      <div className="form-field"><label htmlFor="assign-job">Existing job</label><select id="assign-job" autoFocus required value={jobId} onChange={event => setJobId(event.target.value)}><option value="">Select a job</option>{available.map(job => <option key={job.JOB_ID} value={job.JOB_ID}>{job.JOB_NAME} · {job.JOB_TYPE} / {job.JOB_LAYER || 'GENERAL'}</option>)}</select></div>
      {!available.length && <p className="form-help">All existing jobs are already assigned to this workflow. Create a new job to add another.</p>}
      <p className="form-help">The job is added at the end. Move it into position, then save changes. Jobs can be reused in other workflows.</p>
      <div className="modal-actions"><button className="button" type="button" disabled={!canCreate || busy} title={!canCreate ? 'Save or discard pending changes first' : ''} onClick={onCreate}>+ Create new job</button><button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={!jobId || busy}>Add to workflow</button></div>
    </form>
  </EditorDialog>
}

export default function WorkflowAdministration({ onNavigate, workflowId = '', workflowName = '', initialAction = '', returnContext = null }) {
  const { data: live, acceptSubmission } = useOrchestrationMonitor()
  const [catalog, setCatalog] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [drafts, setDrafts] = useState({})
  const [expanded, setExpanded] = useState(() => new Set(workflowId ? [workflowId] : []))
  const [search, setSearch] = useState(workflowName)
  const openedInitial = useRef(false)
  const [group, setGroup] = useState('')
  const [state, setState] = useState('')
  const [dialog, setDialog] = useState(null)
  const [dialogError, setDialogError] = useState('')
  const [dragging, setDragging] = useState(null)
  const [dropTarget, setDropTarget] = useState(null)
  const [engine, setEngine] = useState(null)
  const selectedLiveWorkflow = dialog?.kind === 'run' ? live?.rows?.find(row => row.WORKFLOW_ID === dialog.workflow.WORKFLOW_ID) : null
  const runWorkflow = selectedLiveWorkflow ? applySubmission(selectedLiveWorkflow, live?.submissions) : dialog?.workflow
  const dirty = Object.keys(drafts).length > 0
  const busy = loading || saving
  const workflows = catalog?.workflows || []
  const jobs = catalog?.jobs || []
  const assignments = catalog?.assignments || []
  const jobById = useMemo(() => new Map(jobs.map(job => [job.JOB_ID, job])), [jobs])
  const workflowById = useMemo(() => new Map(workflows.map(workflow => [workflow.WORKFLOW_ID, workflow])), [workflows])
  const groups = useMemo(() => [...new Set(workflows.map(workflow => workflow.WORKFLOW_GROUP).filter(Boolean))].sort(), [workflows])
  const jobIds = workflowId => drafts[workflowId] || workflowJobIds(assignments, workflowId)
  const query = search.trim().toLowerCase()
  const visible = workflows.filter(workflow => (!group || workflow.WORKFLOW_GROUP === group)
    && (!state || String(workflow.IS_ENABLED) === state)
    && (!query || [workflow.WORKFLOW_NAME, workflow.WORKFLOW_GROUP, ...jobIds(workflow.WORKFLOW_ID).map(id => jobById.get(id)?.JOB_NAME)].some(value => String(value || '').toLowerCase().includes(query))))

  async function load() {
    setLoading(true); setError('')
    try {
      const [configuration, status] = await Promise.allSettled([api.orchestrationConfiguration(), api.orchestrationEngine()])
      if (configuration.status === 'rejected') throw configuration.reason
      setCatalog(configuration.value)
      setEngine(status.status === 'fulfilled' ? status.value : { installed: false, runsEnabled: false, message: 'Parallel engine is unavailable.' })
    }
    catch (failure) { setError(failure.message) }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])
  useEffect(() => {
    if (!catalog || !workflowId || openedInitial.current) return
    openedInitial.current = true
    const record = catalog.workflows.find(workflow => workflow.WORKFLOW_ID === workflowId)
    if (!record) { setError('This workflow is no longer available in the current catalog.'); return }
    setSearch(record.WORKFLOW_NAME)
    if (initialAction === 'edit') setDialog({ kind: 'workflow', record })
  }, [catalog, workflowId, initialAction])
  useEffect(() => {
    if (!dirty) return undefined
    const guard = event => { event.preventDefault(); event.returnValue = '' }
    const navigate = event => { if (!window.confirm('Discard unsaved workflow changes and leave this page?')) event.preventDefault() }
    window.addEventListener('beforeunload', guard)
    window.addEventListener('kumo:before-navigate', navigate)
    return () => { window.removeEventListener('beforeunload', guard); window.removeEventListener('kumo:before-navigate', navigate) }
  }, [dirty])

  function toggle(id) {
    setExpanded(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })
  }
  function changeOrder(workflowId, ids) {
    setDrafts(current => {
      const next = { ...current }
      if (JSON.stringify(ids) === JSON.stringify(workflowJobIds(assignments, workflowId))) delete next[workflowId]
      else next[workflowId] = ids
      return next
    })
    setMessage(''); setError('')
  }
  function move(workflowId, id, position) {
    changeOrder(workflowId, moveJob(jobIds(workflowId), id, position))
  }
  function open(next) { setDialogError(''); setDialog(next) }
  async function saveOrder() {
    if (saving) return
    setSaving(true); setError(''); setMessage('')
    try {
      const data = await api.saveOrchestrationJobOrder({ revision: catalog.revision, orders: Object.entries(drafts).map(([workflowId, ids]) => ({ workflowId, jobIds: ids })) })
      setCatalog(data); setDrafts({}); setMessage('Job assignments and order saved.')
    } catch (failure) { setError(failure.message) }
    finally { setSaving(false) }
  }
  async function saveDefinition(form) {
    if (saving) return
    setSaving(true); setDialogError(''); setMessage('')
    try {
      if (dialog.kind === 'workflow') {
        if (dialog.record) await api.updateOrchestrationWorkflow(dialog.record.WORKFLOW_ID, form)
        else {
          const created = await api.createOrchestrationWorkflow(form)
          setExpanded(current => new Set([...current, created.workflowId]))
        }
      } else if (dialog.record) await api.updateOrchestrationJob(dialog.record.JOB_ID, form)
      else await api.createOrchestrationJob({ ...form, workflowId: dialog.workflow?.WORKFLOW_ID, revision: catalog.revision })
      setDialog(null); setMessage('Saved successfully.'); await load()
    } catch (failure) { setDialogError(failure.message) }
    finally { setSaving(false) }
  }
  async function deleteDefinition() {
    if (saving) return
    setSaving(true); setDialogError('')
    try {
      if (dialog.kind === 'workflow') await api.deleteOrchestrationWorkflow(dialog.record.WORKFLOW_ID)
      else await api.deleteOrchestrationJob(dialog.record.JOB_ID)
      setDialog(null); setMessage('Deleted successfully.'); await load()
    } catch (failure) { setDialogError(failure.message) }
    finally { setSaving(false) }
  }
  function drop(event, workflowId, position) {
    event.preventDefault()
    if (!busy && dragging?.workflowId === workflowId) move(workflowId, dragging.jobId, position)
    setDragging(null); setDropTarget(null)
  }

  return <section className="page om-page wa-page">
    <PageHeader breadcrumb="Workflow / Workflow" title="Workflow" subtitle="Manage workflows and their jobs in execution order." actions={<div className="page-header-actions">{returnContext && <button className="button" type="button" onClick={() => onNavigate('orchestrationMonitor', returnContext)}>← Monitor</button>}<button className="button" type="button" disabled={busy || dirty} onClick={load}>↻ Refresh</button><button className="button" type="button" disabled={busy || dirty || !catalog} onClick={() => open({ kind: 'job' })}>+ Job</button><button className="button primary" type="button" disabled={busy || dirty || !catalog} onClick={() => open({ kind: 'workflow' })}>+ Workflow</button></div>} />
    {error && <div className="alert error" role="alert">{error}</div>}
    {message && <div className="alert success" role="status">{message}</div>}
    {loading && !catalog && <LoadingState>Loading workflows and jobs…</LoadingState>}
    {catalog && <>
      <div className="om-notice wa-notice"><strong>{catalog.source}</strong><span>Workflow and job configuration</span><span className="wa-notice-end">First job → Workflow · Following jobs → Previous job</span></div>
      {engine?.message && <div className="wa-engine-state"><span>{engine.message}</span>{engine.installed && <span>{engine.preparedWorkflows} workflows prepared</span>}</div>}
      {catalog.warnings?.length > 0 && <details className="wa-warnings"><summary>{catalog.warnings.length} dependency reference {catalog.warnings.length === 1 ? 'issue' : 'issues'} to review</summary><p>These rows are preserved. Assignments with missing definitions are excluded from the list below.</p><ul>{catalog.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
      <div className="monitor-toolbar om-toolbar"><input className="search-input" type="search" aria-label="Search workflows, groups or jobs" placeholder="Search workflows, groups or jobs…" value={search} onChange={event => { setSearch(event.target.value); if (event.target.value.trim()) setExpanded(new Set(workflows.map(workflow => workflow.WORKFLOW_ID))) }} /><div className="monitor-toolbar-actions"><select className="status-select" aria-label="Filter workflow group" value={group} onChange={event => setGroup(event.target.value)}><option value="">All groups</option>{groups.map(value => <option key={value}>{value}</option>)}</select><select className="status-select" aria-label="Filter enabled workflows" value={state} onChange={event => setState(event.target.value)}><option value="">All workflows</option><option value="true">Enabled</option><option value="false">Disabled</option></select></div></div>
      <div className="om-summary"><span><strong>{workflows.length}</strong> Workflows</span><span><strong>{jobs.length}</strong> Reusable jobs</span><span><strong>{workflows.reduce((total, workflow) => total + jobIds(workflow.WORKFLOW_ID).length, 0)}</strong> Job assignments</span><span className="om-total" aria-live="polite">Showing {visible.length} of {workflows.length} workflows</span></div>
      <div className="om-list-heading"><span>Workflow / Job hierarchy</span><div><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(new Set(visible.map(workflow => workflow.WORKFLOW_ID)))}>Expand all</button><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(new Set())}>Collapse all</button></div></div>
      {dirty && <div className="wa-save-bar" role="status"><span>Unsaved changes in {Object.keys(drafts).length} {Object.keys(drafts).length === 1 ? 'workflow' : 'workflows'}. Save or discard before editing definitions.</span><div><button className="button" type="button" disabled={saving} onClick={() => { setDrafts({}); setError(''); setMessage('Draft changes discarded.') }}>Discard changes</button><button className="button primary" type="button" disabled={busy} onClick={saveOrder}>{saving ? 'Saving…' : 'Save changes'}</button></div></div>}
      <div className="om-table-wrap"><table className="om-table wa-table"><caption className="visually-hidden">Workflows with expandable jobs. Drag a job or use its move buttons to change order. Save changes to persist assignments and parent links.</caption><colgroup><col className="wa-name-col" /><col className="wa-type-col" /><col className="wa-state-col" /><col /><col className="wa-parent-col" /><col className="wa-order-col" /><col className="wa-actions-col" /></colgroup><thead><tr>{['Workflow / Job', 'Type / Layer', 'State', 'Schedule / Command', 'Parent', 'Order', 'Actions'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
        {visible.map(workflow => {
          const id = workflow.WORKFLOW_ID, ids = jobIds(id), isExpanded = expanded.has(id)
          const locked = busy || catalog.invalidWorkflows?.includes(id)
          const liveWorkflow = (live?.rows || []).find(row => row.WORKFLOW_ID === id)
          const current = liveWorkflow ? applySubmission(liveWorkflow, live?.submissions) : null
          const active = current && activeWorkflowStatuses.has(current.run.status)
          const runUnavailable = active ? 'Workflow is already queued or running.' : !engine?.runsEnabled ? engine?.message || 'Parallel engine is unavailable.'
            : !workflow.ALLOW_MANUAL_TRIGGER ? 'Manual execution is disabled for this workflow.' : ''
          const header = catalog.rows.find(row => row.id === id)
          const parentWorkflow = workflowById.get(header?.parentId)
          return <Fragment key={id}>
            <tbody className="om-parent"><tr><th scope="row"><div className="om-name"><HierarchyToggle expanded={isExpanded} aria-controls={`wa-jobs-${id}`} aria-label={`${isExpanded ? 'Hide' : 'Show'} jobs for ${workflow.WORKFLOW_NAME}`} onClick={() => toggle(id)} /><div><strong>{workflow.WORKFLOW_NAME}</strong><small>{workflow.WORKFLOW_GROUP || 'Ungrouped'}</small></div></div></th><td><span className="om-kind">Workflow</span><small>{ids.length} {ids.length === 1 ? 'job' : 'jobs'}</small></td><td><State enabled={workflow.IS_ENABLED} /></td><td>{workflow.SCHEDULE ? <><span>{workflow.SCHEDULE}</span><small>{workflow.SCHEDULE_TIMEZONE || 'UTC'} · {workflow.IS_SCHEDULE_ENABLED ? 'Enabled' : 'Disabled'}</small></> : <span className="om-muted">{workflow.ALLOW_EXTERNAL_TRIGGER ? 'External trigger' : 'Manual trigger'}</span>}</td><td><span className="om-dependency">{parentWorkflow?.WORKFLOW_NAME || (header?.parentId ? 'Unknown parent' : 'No parent')}</span></td><td><span className={drafts[id] ? 'wa-pending' : 'om-muted'}>{drafts[id] ? 'Unsaved' : '—'}</span></td><td><div className="wa-actions"><button className="button" type="button" disabled={busy || dirty} aria-label={`Edit workflow ${workflow.WORKFLOW_NAME}`} onClick={() => open({ kind: 'workflow', record: workflow })}>Edit</button><button className="button" type="button" disabled={busy || dirty || locked || !!runUnavailable || !workflow.IS_ENABLED || !ids.length} title={runUnavailable} aria-label={`Run workflow ${workflow.WORKFLOW_NAME}`} onClick={() => open({ kind: 'run', workflow })}>Run</button><button className="button" type="button" disabled={!engine?.installed} aria-label={`Execution history for ${workflow.WORKFLOW_NAME}`} onClick={() => onNavigate('orchestrationHistory', { workflowId: workflow.WORKFLOW_ID, workflowName: workflow.WORKFLOW_NAME })}>History</button><button className="button" type="button" disabled={locked} aria-label={`Add job to ${workflow.WORKFLOW_NAME}`} onClick={() => { setExpanded(current => new Set([...current, id])); open({ kind: 'assign', workflow }) }}>+ Job</button></div></td></tr></tbody>
            <tbody id={`wa-jobs-${id}`} className="om-children" hidden={!isExpanded}>
              {ids.map((jobId, position) => {
                const job = jobById.get(jobId)
                if (!job) return null
                const savedAssignment = assignments.find(row => row.WORKFLOW_ID === id && row.JOB_ID === jobId)
                const isEntry = drafts[id] ? position === 0 : savedAssignment?.PARENT_WORKFLOW_ID === id
                const parentName = drafts[id]
                  ? (position === 0 ? workflow.WORKFLOW_NAME : jobById.get(ids[position - 1])?.JOB_NAME)
                  : (isEntry ? workflow.WORKFLOW_NAME : jobById.get(savedAssignment?.PARENT_JOB_ID)?.JOB_NAME || 'Unknown parent')
                return <tr key={jobId} className={dropTarget === `${id}:${jobId}` ? 'wa-drop-target' : ''} onDragOver={event => { if (!locked && dragging?.workflowId === id) { event.preventDefault(); setDropTarget(`${id}:${jobId}`) } }} onDrop={event => drop(event, id, position)}>
                  <th scope="row"><div className="om-job-name"><button className="wa-drag" type="button" draggable={!locked} disabled={locked} aria-label={`Drag ${job.JOB_NAME} to reorder`} title="Drag to reorder, or use the move buttons" onDragStart={event => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', jobId); setDragging({ workflowId: id, jobId }) }} onDragEnd={() => { setDragging(null); setDropTarget(null) }}>⠿</button><span className="om-sequence">{String(position + 1).padStart(2, '0')}</span><div><strong>{job.JOB_NAME}</strong>{job.DESCRIPTION && <small>{job.DESCRIPTION}</small>}</div></div></th>
                  <td><span className="om-kind">{job.JOB_TYPE}</span><small>{job.JOB_LAYER || 'GENERAL'}</small></td><td><State enabled={job.IS_ENABLED} /></td><td><span className="wa-command" title={job.SQL_COMMAND || job.DBT_COMMAND || job.COMMAND_LINE || ''}>{job.SQL_COMMAND || job.DBT_COMMAND || job.COMMAND_LINE || '—'}</span></td><td><span className="om-dependency">{parentName}</span><small>{isEntry ? 'Workflow · First job' : 'Job parent'}</small></td>
                  <td><div className="wa-order"><button className="button" type="button" disabled={locked || position === 0} aria-label={`Move ${job.JOB_NAME} up in ${workflow.WORKFLOW_NAME}`} onClick={() => move(id, jobId, position - 1)}>↑</button><button className="button" type="button" disabled={locked || position === ids.length - 1} aria-label={`Move ${job.JOB_NAME} down in ${workflow.WORKFLOW_NAME}`} onClick={() => move(id, jobId, position + 1)}>↓</button></div></td>
                  <td><div className="wa-actions">{job.JOB_TYPE === 'DBT' && <button className="button" type="button" disabled={busy || dirty} aria-label={`DAG Preview for ${job.JOB_NAME}`} onClick={() => open({ kind: 'preview', record: job })}>DAG Preview</button>}<button className="button" type="button" disabled={busy || dirty} aria-label={`Edit job ${job.JOB_NAME}`} onClick={() => open({ kind: 'job', record: job })}>Edit</button><button className="button" type="button" disabled={busy || dirty || locked || !!runUnavailable || !workflow.IS_ENABLED || !job.IS_ENABLED} title={runUnavailable} aria-label={`Run ${job.JOB_NAME} in ${workflow.WORKFLOW_NAME}`} onClick={() => open({ kind: 'run', workflow, jobId })}>Run…</button><button className="button wa-remove" type="button" disabled={locked} aria-label={`Remove ${job.JOB_NAME} from ${workflow.WORKFLOW_NAME}`} title="Remove assignment; keep the reusable job" onClick={() => changeOrder(id, ids.filter(value => value !== jobId))}>×</button></div></td>
                </tr>
              })}
              {!ids.length && <tr><td colSpan={7} className="om-empty-jobs">No jobs assigned. <button className="button" type="button" disabled={locked} onClick={() => open({ kind: 'assign', workflow })}>+ Add first job</button></td></tr>}
            </tbody>
          </Fragment>
        })}
        {!visible.length && <tbody><tr><td colSpan={7} className="om-empty-jobs">{workflows.length ? 'No workflows match your filters.' : 'Create your first workflow to start adding jobs.'}</td></tr></tbody>}
      </table></div><p className="om-footnote">Drag jobs or use ↑ / ↓ to change their order. The first job always has its workflow as parent; each following job has the previous job as parent.</p>
    </>}
    {dialog?.kind === 'preview' && <DagPreviewDialog definitionId={dialog.record.JOB_ID} name={dialog.record.JOB_NAME} orchestration onClose={() => setDialog(null)} />}
    {dialog?.kind === 'assign' && <AssignJob workflow={dialog.workflow} jobs={jobs} assigned={jobIds(dialog.workflow.WORKFLOW_ID)} busy={busy} canCreate={!dirty} onClose={() => setDialog(null)} onAdd={jobId => { const id = dialog.workflow.WORKFLOW_ID; changeOrder(id, [...jobIds(id), jobId]); setDialog(null) }} onCreate={() => open({ kind: 'job', workflow: dialog.workflow })} />}
    {dialog?.kind === 'workflow' && <WorkflowEditor workflow={dialog.record} groups={groups} jobs={jobs} assignments={assignments} saving={saving} error={dialogError} onClose={() => !saving && setDialog(null)} onSave={saveDefinition} onDelete={deleteDefinition} />}
    {dialog?.kind === 'job' && <JobEditor job={dialog.record} workflowName={dialog.workflow?.WORKFLOW_NAME} assignments={assignments} saving={saving} error={dialogError} onClose={() => !saving && setDialog(null)} onSave={saveDefinition} onDelete={deleteDefinition} />}
    {dialog?.kind === 'run' && <RunWorkflowDialog workflow={runWorkflow} jobId={dialog.jobId} jobs={jobIds(dialog.workflow.WORKFLOW_ID).map(id => jobById.get(id))} onClose={() => setDialog(null)} onAccepted={result => { acceptSubmission(result); setDialog(null) }} />}
  </section>
}
