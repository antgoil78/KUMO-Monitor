import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import './OrchestrationCatalog.css'

function EnabledState({ enabled }) {
  return <span className={`orchestration-state ${enabled ? 'enabled' : 'disabled'}`}><i />{enabled ? 'Enabled' : 'Disabled'}</span>
}

function formatAuditDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

const WORKFLOW_SORT_COLUMNS = [
  ['WORKFLOW_NAME', 'Name'],
  ['WORKFLOW_GROUP', 'Workflow group'],
  ['SCHEDULE', 'Schedule'],
  ['CREATED_AT', 'Created at'],
  ['CREATED_BY', 'Created by'],
  ['UPDATED_AT', 'Updated at'],
  ['UPDATED_BY', 'Updated by']
]

function workflowSortValue(workflow, key) {
  if (key === 'CREATED_AT' || key === 'UPDATED_AT') {
    const value = new Date(workflow[key] || 0).getTime()
    return Number.isNaN(value) ? 0 : value
  }
  return String(workflow[key] || '').toLocaleLowerCase()
}

function WorkflowCatalogTable({ workflows, onEdit, onCreate }) {
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState({ key: 'WORKFLOW_NAME', direction: 'asc' })
  const visibleWorkflows = useMemo(() => {
    const query = search.trim().toLocaleLowerCase()
    const filtered = query
      ? workflows.filter(workflow => WORKFLOW_SORT_COLUMNS.some(([key]) => String(workflow[key] || '').toLocaleLowerCase().includes(query)))
      : workflows
    return [...filtered].sort((left, right) => {
      const leftValue = workflowSortValue(left, sort.key)
      const rightValue = workflowSortValue(right, sort.key)
      const comparison = typeof leftValue === 'number' ? leftValue - rightValue : leftValue.localeCompare(rightValue)
      return (sort.direction === 'asc' ? comparison : -comparison) || String(left.WORKFLOW_NAME).localeCompare(String(right.WORKFLOW_NAME))
    })
  }, [search, sort, workflows])

  function changeSort(key) {
    setSort(current => current.key === key
      ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
      : { key, direction: 'asc' })
  }

  return <div className="orchestration-workflow-list">
    <div className="orchestration-list-toolbar">
      <input className="search-input" type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search workflows or groups…" aria-label="Search workflows" />
      <span>{visibleWorkflows.length} of {workflows.length} workflows</span>
      <button className="button primary" type="button" onClick={onCreate}>+ Workflow</button>
    </div>
    <div className="orchestration-table-wrap">
      <table className="orchestration-table orchestration-workflow-table">
        <thead><tr>
          {WORKFLOW_SORT_COLUMNS.map(([key, label]) => <th key={key} aria-sort={sort.key === key ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}>
            <button className="orchestration-sort-button" type="button" onClick={() => changeSort(key)}>{label}<span aria-hidden="true">{sort.key === key ? (sort.direction === 'asc' ? '↑' : '↓') : '↕'}</span></button>
          </th>)}
          <th className="orchestration-actions-cell">Actions</th>
        </tr></thead>
        <tbody>{visibleWorkflows.map(workflow => <tr key={workflow.WORKFLOW_ID}>
          <td><strong>{workflow.WORKFLOW_NAME}</strong></td>
          <td><span className="orchestration-type">{workflow.WORKFLOW_GROUP || 'Ungrouped'}</span></td>
          <td>{workflow.SCHEDULE ? <><strong>{workflow.SCHEDULE}</strong><small>{workflow.SCHEDULE_TIMEZONE || 'UTC'}{workflow.IS_SCHEDULE_ENABLED ? '' : ' · Disabled'}</small></> : <span className="orchestration-muted">Not scheduled</span>}</td>
          <td>{formatAuditDate(workflow.CREATED_AT)}</td>
          <td>{workflow.CREATED_BY || '—'}</td>
          <td>{formatAuditDate(workflow.UPDATED_AT)}</td>
          <td>{workflow.UPDATED_BY || '—'}</td>
          <td className="orchestration-actions-cell"><button className="orchestration-edit" type="button" onClick={() => onEdit(workflow)}>Edit</button></td>
        </tr>)}</tbody>
      </table>
      {!visibleWorkflows.length && <div className="soft-empty">No workflows match your search.</div>}
    </div>
  </div>
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

function WorkflowEditor({ workflow, groups, jobs, assignments, saving, error, onClose, onSave, onDelete }) {
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
    jobIds: existingJobs
  })
  const [groupSelection, setGroupSelection] = useState(initialGroup)
  const [deleteWarning, setDeleteWarning] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  function update(field, value) { setForm(current => ({ ...current, [field]: value })) }
  function selectGroup(value) {
    setGroupSelection(value)
    update('workflowGroup', value === '__new_group__' ? '' : value)
  }
  function toggleJob(jobId) {
    update('jobIds', form.jobIds.includes(jobId) ? form.jobIds.filter(id => id !== jobId) : [...form.jobIds, jobId])
  }
  function moveJob(index, direction) {
    const target = index + direction
    if (target < 0 || target >= form.jobIds.length) return
    const next = [...form.jobIds]
    ;[next[index], next[target]] = [next[target], next[index]]
    update('jobIds', next)
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

  return <div className="modal-backdrop" role="dialog" aria-modal="true" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <form className="vision-modal wide edit-form" onSubmit={event => { event.preventDefault(); onSave(form) }}>
      <div className="modal-header"><div><h2>{workflow ? 'Edit workflow' : 'Create workflow'}</h2><p>Configuration only. This will not create or alter a Snowflake task.</p></div><button className="modal-close" type="button" onClick={onClose}>×</button></div>
      {error && <div className="alert error">{error}</div>}
      {deleteWarning && <div className={`alert ${existingJobs.length ? 'warning' : 'error'}`}>{deleteWarning}</div>}
      <div className="form-grid two">
        <div className="form-field"><label>Workflow name</label><input required value={form.workflowName} onChange={event => update('workflowName', event.target.value)} /></div>
        <div className="form-field"><label>Workflow group</label><select required value={groupSelection} onChange={event => selectGroup(event.target.value)}>
          {!workflow && <option value="" disabled>Select workflow group</option>}
          {workflow && !initialGroup && <option value="">Ungrouped</option>}
          {groups.map(group => <option key={group} value={group}>{group}</option>)}
          {!workflow && <option value="__new_group__">+ Create new group</option>}
        </select></div>
      </div>
      {!workflow && groupSelection === '__new_group__' && <div className="form-field orchestration-new-group"><label>New workflow group</label><input required autoFocus value={form.workflowGroup} onChange={event => update('workflowGroup', event.target.value)} placeholder="Enter a new group name" /></div>}
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
      <div className="form-field"><label>Jobs and execution order</label><div className="orchestration-assignment-editor">
        {form.jobIds.map((jobId, index) => {
          const job = jobs.find(item => item.JOB_ID === jobId)
          return <div key={jobId}><b>{index + 1}</b><span>{job?.JOB_NAME || jobId}</span><button type="button" onClick={() => moveJob(index, -1)} disabled={index === 0}>↑</button><button type="button" onClick={() => moveJob(index, 1)} disabled={index === form.jobIds.length - 1}>↓</button><button type="button" onClick={() => toggleJob(jobId)}>Remove</button></div>
        })}
        {jobs.filter(job => !form.jobIds.includes(job.JOB_ID)).map(job => <button className="orchestration-add-job" key={job.JOB_ID} type="button" onClick={() => toggleJob(job.JOB_ID)}>+ {job.JOB_NAME}</button>)}
        {!jobs.length && <span className="orchestration-empty">Create a job before creating a workflow.</span>}
      </div><small className="form-help">A workflow without jobs is an inactive placeholder. The same reusable job may belong to multiple workflows.</small></div>
      <div className="modal-actions">{workflow && <button className="button danger orchestration-delete" type="button" disabled={saving} onClick={requestDelete}>{confirmDelete ? 'Confirm delete' : 'Delete'}</button>}<button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save workflow'}</button></div>
    </form>
  </div>
}

function JobEditor({ job, defaultLayer, assignments, saving, error, onClose, onSave, onDelete }) {
  const [form, setForm] = useState({
    jobName: job?.JOB_NAME || '', description: job?.DESCRIPTION || '', jobType: job?.JOB_TYPE || 'SQL', jobLayer: job?.JOB_LAYER || defaultLayer || 'GENERAL',
    sqlCommand: job?.SQL_COMMAND || '', dbtCommand: job?.DBT_COMMAND || '', commandLine: job?.COMMAND_LINE || '',
    dbtTarget: job?.DBT_TARGET || '', dbtWorkspace: job?.DBT_WORKSPACE || '', dbtProjectFqn: job?.DBT_PROJECT_FQN || '',
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
  return <div className="modal-backdrop" role="dialog" aria-modal="true" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <form className="vision-modal edit-form" onSubmit={event => { event.preventDefault(); onSave(form) }}>
      <div className="modal-header"><div><h2>{job ? 'Edit job' : 'Create job'}</h2><p>Reusable executable configuration. No Snowflake task will be created.</p></div><button className="modal-close" type="button" onClick={onClose}>×</button></div>
      {error && <div className="alert error">{error}</div>}
      {deleteWarning && <div className={`alert ${jobAssignments.length ? 'warning' : 'error'}`}>{deleteWarning}</div>}
      <div className="form-grid two"><div className="form-field"><label>Job name</label><input required value={form.jobName} onChange={event => update('jobName', event.target.value)} /></div><div className="form-field"><label>Job type</label><select value={form.jobType} onChange={event => update('jobType', event.target.value)}><option value="SQL">SQL</option><option value="DBT">DBT</option><option value="COMMAND">Command</option></select></div></div>
      <div className="form-field"><label>Job layer</label><div className="orchestration-fixed-value">{form.jobLayer}</div></div>
      <div className="form-field"><label>Description</label><textarea rows="2" value={form.description} onChange={event => update('description', event.target.value)} /></div>
      {form.jobType === 'SQL' && <div className="form-field"><label>SQL command</label><textarea required rows="5" value={form.sqlCommand} onChange={event => update('sqlCommand', event.target.value)} /></div>}
      {form.jobType === 'COMMAND' && <div className="form-field"><label>Command line</label><textarea required rows="4" value={form.commandLine} onChange={event => update('commandLine', event.target.value)} /></div>}
      {form.jobType === 'DBT' && <><div className="form-field"><label>DBT command</label><textarea required rows="4" value={form.dbtCommand} onChange={event => update('dbtCommand', event.target.value)} /></div><div className="form-grid two"><div className="form-field"><label>DBT target</label><input value={form.dbtTarget} onChange={event => update('dbtTarget', event.target.value)} /></div><div className="form-field"><label>DBT workspace</label><input value={form.dbtWorkspace} onChange={event => update('dbtWorkspace', event.target.value)} /></div></div><div className="form-field"><label>DBT project FQN</label><input value={form.dbtProjectFqn} onChange={event => update('dbtProjectFqn', event.target.value)} /></div></>}
      <div className="form-grid two"><div className="form-field"><label>Timeout seconds</label><input type="number" min="1" value={form.timeoutSeconds} onChange={event => update('timeoutSeconds', event.target.value)} /></div><div className="toggle-row"><label><input type="checkbox" checked={form.enabled} onChange={event => update('enabled', event.target.checked)} /> Job enabled</label></div></div>
      <div className="modal-actions">{job && <button className="button danger orchestration-delete" type="button" disabled={saving} onClick={requestDelete}>{confirmDelete ? 'Confirm delete' : 'Delete'}</button>}<button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save job'}</button></div>
    </form>
  </div>
}

function OrchestrationCatalog({ section }) {
  const [catalog, setCatalog] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [editorDefaults, setEditorDefaults] = useState({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const workflowSection = section === 'workflow'

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
      if (workflowSection) {
        if (editing) await api.updateOrchestrationWorkflow(editing.WORKFLOW_ID, payload)
        else await api.createOrchestrationWorkflow(payload)
      } else if (editing) await api.updateOrchestrationJob(editing.JOB_ID, payload)
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
      if (workflowSection) await api.deleteOrchestrationWorkflow(editing.WORKFLOW_ID)
      else await api.deleteOrchestrationJob(editing.JOB_ID)
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

  const rows = workflowSection ? catalog?.workflows || [] : catalog?.jobs || []
  const workflowGroups = useMemo(() => [...new Set((catalog?.workflows || [])
    .map(workflow => String(workflow.WORKFLOW_GROUP || '').trim())
    .filter(Boolean))].sort((left, right) => left.localeCompare(right)), [catalog?.workflows])
  return <section className="page orchestration-page">
    <PageHeader
      breadcrumb={`Orchestration / ${workflowSection ? 'Workflow' : 'Job'}`}
      title={workflowSection ? 'Workflow' : 'Job'}
      subtitle={workflowSection ? 'Workflow containers, triggers and ordered job assignments in the new orchestration model.' : 'Reusable executable jobs and the workflows that currently use them.'}
      actions={<div className="page-header-actions"><button className="button" type="button" onClick={load} disabled={loading}>↻ Refresh</button></div>}
    />
    <div className="orchestration-context"><span>New orchestration model</span><strong>KUMO_TST.MONITOR_APP</strong><small>Configuration only · Snowflake tasks are not created yet</small></div>
    {error && <div className="alert error">{error}</div>}
    {loading && !catalog && <LoadingState>Loading orchestration catalog…</LoadingState>}
    {catalog && workflowSection && <WorkflowCatalogTable workflows={catalog.workflows} onEdit={record => openEditor(record)} onCreate={() => openEditor()} />}
    {catalog && !workflowSection && rows.length === 0 && <div className="soft-empty">No jobs have been created.</div>}
    {catalog && !workflowSection && rows.length > 0 && <JobCatalogTable jobs={catalog.jobs} onEdit={record => openEditor(record)} onCreate={layer => openEditor(null, { jobLayer: layer })} />}
    {editorOpen && workflowSection && <WorkflowEditor key={editing?.WORKFLOW_ID || 'new-workflow'} workflow={editing} groups={workflowGroups} jobs={catalog?.jobs || []} assignments={catalog?.assignments || []} saving={saving} error={saveError} onClose={() => setEditorOpen(false)} onSave={save} onDelete={remove} />}
    {editorOpen && !workflowSection && <JobEditor key={editing?.JOB_ID || `new-job-${editorDefaults.jobLayer || 'general'}`} job={editing} defaultLayer={editorDefaults.jobLayer} assignments={catalog?.assignments || []} saving={saving} error={saveError} onClose={() => setEditorOpen(false)} onSave={save} onDelete={remove} />}
  </section>
}

export function WorkflowCatalog() {
  return <OrchestrationCatalog section="workflow" />
}

export function JobCatalog() {
  return <OrchestrationCatalog section="job" />
}
