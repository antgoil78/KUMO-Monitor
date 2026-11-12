import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import LoadingState from '../components/LoadingState.jsx'
import CommandPreview from '../components/CommandPreview.jsx'
import PageHeader from '../components/PageHeader.jsx'
import EditorDialog from '../components/EditorDialog.jsx'
import './OrchestrationCatalog.css'

function EnabledState({ enabled }) {
  return <span className={`orchestration-state ${enabled ? 'enabled' : 'disabled'}`}><i />{enabled ? 'Enabled' : 'Disabled'}</span>
}

function formatAuditDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function JobCatalogTable({ jobs, onEdit, onCreate }) {
  const layerOrder = ['RAW', 'SDL', 'EDV', 'BDL', 'GENERAL']
  const layers = useMemo(() => {
    const grouped = jobs.reduce((result, job) => {
      const layer = String(job.JOB_LAYER || 'GENERAL').toUpperCase()
      result.set(layer, [...(result.get(layer) || []), job])
      return result
    }, new Map())
    return [...grouped.entries()]
      .sort(([left], [right]) => {
        const leftIndex = layerOrder.indexOf(left)
        const rightIndex = layerOrder.indexOf(right)
        return (leftIndex < 0 ? layerOrder.length : leftIndex) - (rightIndex < 0 ? layerOrder.length : rightIndex) || left.localeCompare(right)
      })
      .map(([name, rows]) => ({ name, jobs: rows.sort((left, right) => String(left.JOB_NAME).localeCompare(String(right.JOB_NAME))) }))
  }, [jobs])
  const [expandedLayers, setExpandedLayers] = useState(() => new Set())

  function toggleLayer(layerName) {
    setExpandedLayers(current => {
      const next = new Set(current)
      if (next.has(layerName)) next.delete(layerName)
      else next.add(layerName)
      return next
    })
  }

  return <div className="orchestration-workflow-groups orchestration-job-groups">
    <div className="orchestration-workflow-groups-header">Job layer</div>
    {layers.map(layer => {
      const expanded = expandedLayers.has(layer.name)
      return <section className={`orchestration-workflow-group ${expanded ? 'expanded' : ''}`} key={layer.name}>
        <div className="orchestration-workflow-group-header">
          <button className="orchestration-workflow-group-toggle" type="button" aria-expanded={expanded} onClick={() => toggleLayer(layer.name)}>
            <span className="orchestration-workflow-group-chevron" aria-hidden="true">›</span>
            <strong>{layer.name}</strong>
            <b>{layer.jobs.length} {layer.jobs.length === 1 ? 'job' : 'jobs'}</b>
          </button>
          <button className="orchestration-group-create" type="button" onClick={() => onCreate(layer.name)}>+ Job</button>
        </div>
        {expanded && <div className="orchestration-table-wrap orchestration-workflow-group-content">
          <table className="orchestration-table orchestration-job-table">
            <thead><tr><th>Job</th><th>Type</th><th>Created by</th><th>Created</th><th>Updated by</th><th>Updated</th><th>Status</th><th className="orchestration-actions-cell">Actions</th></tr></thead>
            <tbody>{layer.jobs.map(job => <tr key={job.JOB_ID}>
                <td><strong>{job.JOB_NAME}</strong></td>
                <td><span className="orchestration-type">{job.JOB_TYPE}</span></td>
                <td>{job.CREATED_BY || '—'}</td>
                <td>{formatAuditDate(job.CREATED_AT)}</td>
                <td>{job.UPDATED_BY || '—'}</td>
                <td>{formatAuditDate(job.UPDATED_AT)}</td>
                <td><EnabledState enabled={job.IS_ENABLED} /></td>
                <td className="orchestration-actions-cell"><button className="orchestration-edit" type="button" onClick={() => onEdit(job)}>Edit</button></td>
              </tr>)}</tbody>
          </table>
        </div>}
      </section>
    })}
  </div>
}

export function WorkflowEditor({ workflow, groups, jobs, assignments, saving, error, onClose, onSave, onDelete }) {
  const existingJobs = workflow ? assignments.filter(item => item.WORKFLOW_ID === workflow.WORKFLOW_ID).sort((a, b) => a.JOB_SEQUENCE - b.JOB_SEQUENCE).map(item => item.JOB_ID) : []
  const initialGroup = String(workflow?.WORKFLOW_GROUP || '')
  const [form, setForm] = useState({
    workflowName: workflow?.WORKFLOW_NAME || '',
    workflowGroup: initialGroup,
    description: workflow?.DESCRIPTION || '',
    schedule: workflow?.SCHEDULE || '',
    scheduleTimezone: workflow?.SCHEDULE_TIMEZONE || 'UTC',
    scheduleEnabled: Boolean(workflow?.IS_SCHEDULE_ENABLED),
    allowManualTrigger: workflow ? Boolean(workflow.ALLOW_MANUAL_TRIGGER) : true,
    allowExternalTrigger: Boolean(workflow?.ALLOW_EXTERNAL_TRIGGER),
    enabled: workflow ? Boolean(workflow.IS_ENABLED) : true,
    entryJobId: ''
  })
  const [groupSelection, setGroupSelection] = useState(initialGroup)
  const [deleteWarning, setDeleteWarning] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  function update(field, value) { setForm(current => ({ ...current, [field]: value })) }
  function selectGroup(value) {
    setGroupSelection(value)
    update('workflowGroup', value === '__new_group__' ? '' : value)
  }
  function requestDelete() {
    if (existingJobs.length) {
      setConfirmDelete(false)
      setDeleteWarning(`This workflow cannot be deleted because it contains ${existingJobs.length} ${existingJobs.length === 1 ? 'job' : 'jobs'}. Remove every job and save the workflow first.`)
      return
    }
    if (!confirmDelete) {
      setConfirmDelete(true)
      setDeleteWarning('Deleting this workflow cannot be undone. Confirm deletion to continue.')
      return
    }
    onDelete()
  }

  return <EditorDialog title={workflow ? 'Edit workflow' : 'Create workflow'} busy={saving} onClose={onClose}>
    <form className="vision-modal wide edit-form" onSubmit={event => { event.preventDefault(); onSave(form) }}>
      <div className="modal-header"><div><h2>{workflow ? 'Edit workflow' : 'Create workflow'}</h2><p>Workflow settings and trigger configuration.</p></div><button className="modal-close" type="button" disabled={saving} aria-label="Close dialog" onClick={onClose}>×</button></div>
      {error && <div className="alert error">{error}</div>}
      {deleteWarning && <div className={`alert ${existingJobs.length ? 'warning' : 'error'}`}>{deleteWarning}</div>}
      <div className="form-grid two">
        <div className="form-field"><label htmlFor="workflow-name">Workflow name</label><input id="workflow-name" required value={form.workflowName} onChange={event => update('workflowName', event.target.value)} /></div>
        <div className="form-field"><label htmlFor="workflow-group">Workflow group</label><select id="workflow-group" value={groupSelection} onChange={event => selectGroup(event.target.value)}>
          <option value="">Ungrouped</option>
          {groups.map(group => <option key={group} value={group}>{group}</option>)}
          <option value="__new_group__">+ Create new group</option>
        </select></div>
      </div>
      {groupSelection === '__new_group__' && <div className="form-field orchestration-new-group"><label htmlFor="new-workflow-group">New workflow group</label><input id="new-workflow-group" required autoFocus value={form.workflowGroup} onChange={event => update('workflowGroup', event.target.value)} placeholder="Enter a new group name" /></div>}
      <div className="form-field"><label>Description</label><textarea rows="2" value={form.description} onChange={event => update('description', event.target.value)} /></div>
      <div className="form-grid two">
        <div className="form-field"><label>Schedule cron</label><input placeholder="0 6 * * 1-5" value={form.schedule} onChange={event => update('schedule', event.target.value)} /></div>
        <div className="form-field"><label>Schedule timezone</label><input value={form.scheduleTimezone} onChange={event => update('scheduleTimezone', event.target.value)} /></div>
      </div>
      <div className="toggle-row orchestration-toggles">
        <label><input type="checkbox" checked={form.scheduleEnabled} onChange={event => update('scheduleEnabled', event.target.checked)} /> Schedule enabled</label>
        <label><input type="checkbox" checked={form.allowManualTrigger} onChange={event => update('allowManualTrigger', event.target.checked)} /> Manual trigger</label>
        <label><input type="checkbox" checked={form.allowExternalTrigger} onChange={event => update('allowExternalTrigger', event.target.checked)} /> External trigger</label>
        <label><input type="checkbox" checked={form.enabled} onChange={event => update('enabled', event.target.checked)} /> Workflow enabled</label>
      </div>
      {!workflow && <div className="form-field"><label>Entry job</label><select value={form.entryJobId} onChange={event => update('entryJobId', event.target.value)}><option value="">Add the first job later</option>{jobs.map(job => <option key={job.JOB_ID} value={job.JOB_ID}>{job.JOB_NAME}</option>)}</select><small className="form-help">Optional. The first assigned job always has this workflow as its parent.</small></div>}
      {workflow && <div className="form-field"><label>Assigned jobs</label><div className="orchestration-fixed-value">{existingJobs.length} jobs</div><small className="form-help">Add jobs and change their order directly on the Workflow page.</small></div>}
      <div className="modal-actions">{workflow && <button className="button danger orchestration-delete" type="button" disabled={saving} onClick={requestDelete}>{confirmDelete ? 'Confirm delete' : 'Delete'}</button>}<button className="button" type="button" disabled={saving} onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save workflow'}</button></div>
    </form>
  </EditorDialog>
}

export function JobEditor({ job, defaultLayer, workflowName, assignments, saving, error, onClose, onSave, onDelete }) {
  const [form, setForm] = useState({
    jobName: job?.JOB_NAME || '', description: job?.DESCRIPTION || '', jobType: job?.JOB_TYPE || 'SQL', jobLayer: job?.JOB_LAYER || defaultLayer || 'GENERAL',
    sqlCommand: job?.SQL_COMMAND || '', dbtCommand: job?.DBT_COMMAND || '', commandLine: job?.COMMAND_LINE || '',
    dbtTarget: job?.DBT_TARGET || '[DBT_TARGET]', dbtWorkspace: job?.DBT_WORKSPACE || '', dbtProjectFqn: job?.DBT_PROJECT_FQN || '[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]',
    timeoutSeconds: job?.TIMEOUT_SECONDS || '', enabled: job ? Boolean(job.IS_ENABLED) : true
  })
  const jobAssignments = job ? assignments.filter(item => item.JOB_ID === job.JOB_ID) : []
  const [deleteWarning, setDeleteWarning] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  function update(field, value) { setForm(current => ({ ...current, [field]: value })) }
  function requestDelete() {
    if (jobAssignments.length) {
      setConfirmDelete(false)
      setDeleteWarning(`This job cannot be deleted because it is used by ${jobAssignments.length} ${jobAssignments.length === 1 ? 'workflow' : 'workflows'}. Remove the job from every workflow first.`)
      return
    }
    if (!confirmDelete) {
      setConfirmDelete(true)
      setDeleteWarning('Deleting this job cannot be undone. Confirm deletion to continue.')
      return
    }
    onDelete()
  }
  return <EditorDialog title={job ? 'Edit job' : 'Create job'} busy={saving} onClose={onClose}>
    <form className="vision-modal edit-form" onSubmit={event => { event.preventDefault(); onSave(form) }}>
      <div className="modal-header"><div><h2>{job ? 'Edit job' : 'Create job'}</h2><p>{workflowName ? `Create and append to ${workflowName}.` : 'Reusable executable configuration.'}{job && jobAssignments.length > 1 ? ` Changes apply to all ${jobAssignments.length} workflows using this job.` : ''}</p></div><button className="modal-close" type="button" disabled={saving} aria-label="Close dialog" onClick={onClose}>×</button></div>
      {error && <div className="alert error">{error}</div>}
      {deleteWarning && <div className={`alert ${jobAssignments.length ? 'warning' : 'error'}`}>{deleteWarning}</div>}
      <div className="form-grid two"><div className="form-field"><label htmlFor="job-name">Job name</label><input id="job-name" required value={form.jobName} onChange={event => update('jobName', event.target.value)} /></div><div className="form-field"><label htmlFor="job-type">Job type</label><select id="job-type" value={form.jobType} onChange={event => update('jobType', event.target.value)}><option value="SQL">SQL</option><option value="DBT">DBT</option><option value="COMMAND">Command</option></select></div></div>
      <div className="form-field"><label htmlFor="job-layer">Job layer</label><select id="job-layer" value={form.jobLayer} onChange={event => update('jobLayer', event.target.value)}>{['RAW', 'SDL', 'EDV', 'BDL', 'GENERAL'].map(layer => <option key={layer}>{layer}</option>)}</select></div>
      <div className="form-field"><label>Description</label><textarea rows="2" value={form.description} onChange={event => update('description', event.target.value)} /></div>
      {form.jobType === 'SQL' && <div className="form-field"><label htmlFor="job-sql">SQL command</label><textarea id="job-sql" required rows="5" value={form.sqlCommand} onChange={event => update('sqlCommand', event.target.value)} /></div>}
      {form.jobType === 'COMMAND' && <div className="form-field"><label>Command line</label><textarea required rows="4" value={form.commandLine} onChange={event => update('commandLine', event.target.value)} /></div>}
      {form.jobType === 'DBT' && <><div className="form-field"><label>DBT command</label><textarea required rows="4" value={form.dbtCommand} onChange={event => update('dbtCommand', event.target.value)} /></div><div className="form-grid two"><div className="form-field"><label>DBT target</label><input value={form.dbtTarget} onChange={event => update('dbtTarget', event.target.value)} /></div><div className="form-field"><label>DBT workspace</label><input value={form.dbtWorkspace} onChange={event => update('dbtWorkspace', event.target.value)} /></div></div><div className="form-field"><label>DBT project FQN</label><input value={form.dbtProjectFqn} onChange={event => update('dbtProjectFqn', event.target.value)} /></div></>}
      {form.jobType !== 'COMMAND' && <CommandPreview definition={form} />}
      <div className="form-grid two"><div className="form-field"><label>Timeout seconds</label><input type="number" min="1" value={form.timeoutSeconds} onChange={event => update('timeoutSeconds', event.target.value)} /></div><div className="toggle-row"><label><input type="checkbox" checked={form.enabled} onChange={event => update('enabled', event.target.checked)} /> Job enabled</label></div></div>
      <div className="modal-actions">{job && <button className="button danger orchestration-delete" type="button" disabled={saving} onClick={requestDelete}>{confirmDelete ? 'Confirm delete' : 'Delete'}</button>}<button className="button" type="button" disabled={saving} onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save job'}</button></div>
    </form>
  </EditorDialog>
}

export function JobCatalog() {
  const [catalog, setCatalog] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [editorDefaults, setEditorDefaults] = useState({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')

  async function load() {
    setLoading(true)
    setError('')
    try {
      setCatalog(await api.orchestrationCatalog())
    } catch (loadError) {
      setError(loadError.message || String(loadError))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  function openEditor(record = null, defaults = {}) {
    setEditing(record)
    setEditorDefaults(defaults)
    setSaveError('')
    setEditorOpen(true)
  }

  async function save(payload) {
    setSaving(true)
    setSaveError('')
    try {
      if (editing) await api.updateOrchestrationJob(editing.JOB_ID, payload)
      else await api.createOrchestrationJob(payload)
      setEditorOpen(false)
      setEditing(null)
      setEditorDefaults({})
      await load()
    } catch (saveFailure) {
      setSaveError(saveFailure.message || String(saveFailure))
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    if (!editing) return
    setSaving(true)
    setSaveError('')
    try {
      await api.deleteOrchestrationJob(editing.JOB_ID)
      setEditorOpen(false)
      setEditing(null)
      setEditorDefaults({})
      await load()
    } catch (deleteFailure) {
      setSaveError(deleteFailure.message || String(deleteFailure))
    } finally {
      setSaving(false)
    }
  }

  const rows = catalog?.jobs || []
  return <section className="page orchestration-page">
    <PageHeader
      breadcrumb="Workflow / Job"
      title="Job"
      subtitle="Reusable executable jobs and the workflows that currently use them."
      actions={<div className="page-header-actions"><button className="button" type="button" onClick={load} disabled={loading}>↻ Refresh</button><button className="button primary" type="button" disabled={loading} onClick={() => openEditor()}>+ Job</button></div>}
    />
    <div className="orchestration-context"><span>New orchestration model</span><strong>{catalog?.source || 'Loading catalog…'}</strong><small>Configuration only · Snowflake tasks are not created yet</small></div>
    {error && <div className="alert error">{error}</div>}
    {loading && !catalog && <LoadingState>Loading orchestration catalog…</LoadingState>}
    {catalog && rows.length === 0 && <div className="soft-empty">No jobs have been created.</div>}
    {catalog && rows.length > 0 && <JobCatalogTable jobs={catalog.jobs} onEdit={record => openEditor(record)} onCreate={layer => openEditor(null, { jobLayer: layer })} />}
    {editorOpen && <JobEditor key={editing?.JOB_ID || `new-job-${editorDefaults.jobLayer || 'general'}`} job={editing} defaultLayer={editorDefaults.jobLayer} assignments={catalog?.assignments || []} saving={saving} error={saveError} onClose={() => setEditorOpen(false)} onSave={save} onDelete={remove} />}
  </section>
}
