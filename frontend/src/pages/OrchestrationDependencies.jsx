import { useEffect, useMemo, useRef, useState } from 'react'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { createRunPreview, evaluateDependency, parentOptions, validateConfiguration } from '../utils/orchestrationDependencies.js'
import './OrchestrationDependencies.css'

const outcomeFields = ['success', 'warning', 'error']
const defaults = { trigger: 'DEP', success: null, warning: null, error: null, sql: '', allow: true }
const objectLabel = (row, rows) => row.jobId ? `${rows.find(item => item.id === row.workflowId)?.name} / ${row.name}` : row.name

function Dialog({ title, onClose, children }) {
  const ref = useRef(null)
  useEffect(() => {
    const previous = document.activeElement
    const element = ref.current
    const focusable = () => [...element.querySelectorAll('button, input, select, textarea')].filter(item => !item.disabled)
    focusable()[0]?.focus()
    function onKey(event) {
      if (event.key === 'Escape') onClose()
      if (event.key === 'Tab') {
        const items = focusable(), first = items[0], last = items.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    element.addEventListener('keydown', onKey)
    return () => { element.removeEventListener('keydown', onKey); previous?.focus() }
  }, [onClose])
  return <div className="od-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><div ref={ref} className="od-dialog" role="dialog" aria-modal="true" aria-label={title}><div className="od-dialog-heading"><h2>{title}</h2><button className="button" type="button" aria-label="Close dialog" onClick={onClose}>×</button></div>{children}</div></div>
}

function AddDialog({ rows, jobs, onClose, onAdd }) {
  const workflows = rows.filter(row => !row.jobId)
  const lastJob = workflowId => rows.filter(row => row.workflowId === workflowId && row.jobId).at(-1)?.id || workflowId
  const [form, setForm] = useState({ jobId: '', workflowId: workflows[0]?.id || '', parentId: lastJob(workflows[0]?.id) })
  const [error, setError] = useState('')
  const available = jobs.filter(job => !rows.some(row => row.workflowId === form.workflowId && row.jobId === job.JOB_ID))
  function submit(event) {
    event.preventDefault()
    const job = available.find(item => item.JOB_ID === form.jobId)
    if (!job) { setError('Select a job from the catalog.'); return }
    const addition = { ...defaults, id: crypto.randomUUID(), workflowId: form.workflowId, name: job.JOB_NAME, jobId: job.JOB_ID, parentId: form.parentId }
    onAdd([addition])
  }
  return <Dialog title="Assign existing job to workflow" onClose={onClose}><form onSubmit={submit}>
    {error && <p className="od-form-error" role="alert">{error}</p>}
    <label>Workflow<select aria-label="Workflow" required value={form.workflowId} onChange={event => setForm(current => ({ ...current, workflowId: event.target.value, jobId: '', parentId: lastJob(event.target.value) }))}>{workflows.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
    <label>Job<select aria-label="Job" required value={form.jobId} onChange={event => setForm(current => ({ ...current, jobId: event.target.value }))}><option value="">Select a job</option>{available.map(job => <option key={job.JOB_ID} value={job.JOB_ID}>{job.JOB_NAME}</option>)}</select></label>
    <label>Parent<select aria-label="Parent" required value={form.parentId} onChange={event => setForm(current => ({ ...current, parentId: event.target.value }))}>{rows.filter(row => row.workflowId === form.workflowId).map(row => <option key={row.id} value={row.id}>{row.name}{!row.jobId ? ' (workflow)' : ''}</option>)}</select></label>
    <p className="od-help">Job commands are maintained in the Job catalog. This creates an independent assignment using the existing job definition.</p>
    <div className="od-dialog-actions"><button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit">Assign job</button></div>
  </form></Dialog>
}

function SqlDialog({ row, rows, onClose, onSave }) {
  const [sql, setSql] = useState(row.sql)
  return <Dialog title={`SQL condition · ${objectLabel(row, rows)}`} onClose={onClose}><form onSubmit={event => { event.preventDefault(); onSave(sql.trim()) }}><p className="od-help">This condition is evaluated after the parent status rules pass. It must return one Boolean value.</p><label>SQL condition<textarea rows={8} value={sql} onChange={event => setSql(event.target.value)} placeholder="SELECT TRUE" spellCheck={false} /></label><p className="od-help">SQL is stored as draft text here. Use a simulated result in Run preview to explore its effect.</p><div className="od-dialog-actions"><button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit">Apply condition</button></div></form></Dialog>
}

function RemovalDialog({ row, rows, onClose, onRemove }) {
  return <Dialog title={`Remove ${objectLabel(row, rows)}?`} onClose={onClose}><p>{row.jobId ? 'Remove this job assignment from the draft? Its assignments in other workflows are kept.' : 'Remove this workflow and all its job assignments from the draft?'}</p><div className="od-dialog-actions"><button className="button" type="button" onClick={onClose}>Cancel</button><button className="button danger" type="button" onClick={onRemove}>Remove</button></div></Dialog>
}

function RuleSelect({ row, field, rows, onUpdate }) {
  return <select className={row[field] === true ? 'od-accept' : row[field] === false ? 'od-reject' : ''} aria-label={`${objectLabel(row, rows)} ${field} rule`} value={row[field] === null ? '' : String(row[field])} onChange={event => onUpdate(row.id, field, event.target.value === '' ? null : event.target.value === 'true')}><option value="">Ignore</option><option value="true">Accept</option><option value="false">Reject</option></select>
}

function ConfigurationGrid({ rows, allRows, errors, onUpdate, onSql, onRemove }) {
  const [collapsed, setCollapsed] = useState(new Set())
  function toggle(id) { setCollapsed(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next }) }
  function renderRow(row) {
    const workflow = allRows.find(item => item.id === row.workflowId)
    const children = allRows.filter(item => item.parentId === row.id && (row.jobId || !item.jobId))
    const cannotRemove = !row.jobId || children.length > 0 || row.parentId === row.workflowId
    return <tr key={row.id} className={`${row.jobId ? 'od-job' : 'od-workflow'} ${errors.some(error => error.rowId === row.id) ? 'od-invalid' : ''}`}>
      <th scope="row">{!row.jobId ? <button className="od-workflow-toggle" type="button" aria-expanded={!collapsed.has(row.id)} aria-label={`${collapsed.has(row.id) ? 'Show' : 'Hide'} ${row.name} jobs`} onClick={() => toggle(row.id)}><span aria-hidden="true">{collapsed.has(row.id) ? '›' : '⌄'}</span>{row.name}</button> : <span className="od-job-workflow">{workflow?.name}</span>}</th>
      <td>{row.jobId ? <strong>{row.name}</strong> : <span className="od-dim">—</span>}</td>
      <td><select aria-label={`${objectLabel(row, rows)} parent`} value={row.issue ? '__unresolved__' : row.parentId || ''} onChange={event => onUpdate(row.id, 'parentId', event.target.value || null)}>{row.issue && <option value="__unresolved__" disabled>Choose one parent…</option>}{!row.jobId && <option value="">No parent</option>}{parentOptions(row, allRows).map(parent => <option key={parent.id} value={parent.id}>{parent.name}{!parent.jobId ? ' (workflow)' : ''}</option>)}</select></td>
      <td>{row.jobId ? <span className="od-trigger">DEP</span> : <select aria-label={`${row.name} trigger`} value={row.trigger || ''} onChange={event => onUpdate(row.id, 'trigger', event.target.value || null)}><option value="">External (NULL)</option><option value="DEP">DEP</option><option value="RUNNER">RUNNER · 30s</option></select>}</td>
      {outcomeFields.map(field => <td key={field}><RuleSelect row={row} rows={rows} field={field} onUpdate={onUpdate} /></td>)}
      <td><button className={`od-sql-button ${row.sql ? 'has-sql' : ''}`} type="button" title={row.sql || 'Add a SQL condition'} aria-label={`Edit SQL for ${objectLabel(row, rows)}`} onClick={() => onSql(row)}>{row.sql || '+ Condition'}</button></td>
      <td><select aria-label={`${objectLabel(row, rows)} allow`} value={String(row.allow)} onChange={event => onUpdate(row.id, 'allow', event.target.value === 'true')}><option value="true">TRUE · Go</option><option value="false">FALSE · Skip</option></select></td>
      <td><button className="od-remove" type="button" disabled={cannotRemove} title={cannotRemove ? 'Keep the entry job and remove dependent objects before removing their parent.' : `Remove ${row.name}`} aria-label={`Remove ${objectLabel(row, rows)}`} onClick={() => onRemove(row)}>×</button></td>
    </tr>
  }
  return <div className="od-table-wrap"><table className="od-config-table"><caption className="visually-hidden">Editable workflow and job dependency configuration</caption><colgroup>{[130, 130, 190, 150, 105, 105, 105, 200, 130, 50].map((width, index) => <col key={index} style={{ width }} />)}</colgroup><thead><tr>{['Workflow', 'Job', 'Parent', 'Trigger', 'Success', 'Warning', 'Error', 'SQL', 'Allow', ''].map((name, index) => <th scope="col" key={index}>{name}</th>)}</tr></thead>{rows.filter(row => !row.jobId).map(workflow => <tbody key={workflow.id}>{renderRow(workflow)}{!collapsed.has(workflow.id) && rows.filter(row => row.jobId && row.workflowId === workflow.id).map(renderRow)}</tbody>)}</table></div>
}

function RunPreview({ rows, runs, setRuns, sqlResults, setSqlResults }) {
  function reset() { setRuns(createRunPreview(rows, 'fresh')); setSqlResults({}) }
  return <div className="od-preview">
    <div className="od-preview-heading"><div><h2>Run preview</h2><p>Change run states to see eligibility. Every row has a separate execution record within this preview cycle.</p></div><div className="od-actions"><button className="button" type="button" onClick={reset}>New preview cycle</button></div></div>
    <div className="od-preview-note">Entry jobs start with their workflow. Other dependencies wait for a completed parent. Ignore applies no restriction to that status. Changes here simulate outcomes; nothing is dispatched.</div>
    <div className="od-table-wrap"><table className="od-run-table"><caption className="visually-hidden">Simulated execution states and evaluated dependency decisions</caption><thead><tr>{['Object', 'Run status', 'Parent run', 'SQL result', 'Decision'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{rows.map(row => {
      const evaluation = evaluateDependency(row, rows, runs, sqlResults)
      const parent = rows.find(item => item.id === row.parentId)
      return <tr key={row.id} className={row.jobId ? 'od-job' : 'od-workflow'}>
        <th scope="row"><strong>{objectLabel(row, rows)}</strong><small title={runs[row.id].runId}>{row.jobId ? 'Job execution' : 'Workflow execution'}</small></th>
        <td><select aria-label={`${objectLabel(row, rows)} run status`} value={runs[row.id].status} onChange={event => setRuns(current => ({ ...current, [row.id]: { ...current[row.id], status: event.target.value } }))}>{['WAITING', 'RUNNING', 'SUCCESS', 'WARNING', 'ERROR', 'SKIPPED'].map(status => <option key={status}>{status}</option>)}</select></td>
        <td>{parent ? <><span>{objectLabel(parent, rows)}</span><small><StatusBadge status={runs[parent.id]?.status} /></small></> : <span className="od-dim">External trigger</span>}</td>
        <td>{row.sql ? <select aria-label={`${objectLabel(row, rows)} SQL result`} value={sqlResults[row.id] === undefined ? '' : String(sqlResults[row.id])} onChange={event => setSqlResults(current => ({ ...current, [row.id]: event.target.value === '' ? undefined : event.target.value === 'error' ? 'error' : event.target.value === 'true' }))}><option value="">Not evaluated</option><option value="true">TRUE</option><option value="false">FALSE</option><option value="error">SQL error</option></select> : <span className="od-dim">No condition</span>}</td>
        <td><span className={`od-decision od-${evaluation.state.toLowerCase().replace(' ', '-')}`}>{evaluation.state}</span><small>{evaluation.reason}</small></td>
      </tr>
    })}</tbody></table></div>
    <p className="od-help">Reused jobs have independent execution states in each workflow. A new preview cycle clears all simulated outcomes. Run status is not saved with configuration.</p>
  </div>
}

export default function OrchestrationDependencies({ onNavigate }) {
  const [rows, setRows] = useState([])
  const [saved, setSaved] = useState([])
  const [catalog, setCatalog] = useState(null)
  const [revision, setRevision] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [view, setView] = useState('configuration')
  const [dialog, setDialog] = useState(null)
  const [message, setMessage] = useState('')
  const [runs, setRuns] = useState({})
  const [sqlResults, setSqlResults] = useState({})
  const errors = useMemo(() => rows.length ? validateConfiguration(rows) : [], [rows])
  const dirty = JSON.stringify(rows) !== JSON.stringify(saved)
  const workflowCount = rows.filter(row => !row.jobId).length
  const visible = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return rows
    const ids = new Set(rows.filter(row => objectLabel(row, rows).toLowerCase().includes(query)).map(row => row.workflowId))
    return rows.filter(row => ids.has(row.workflowId))
  }, [rows, search])
  function accept(data) {
    setRows(data.rows); setSaved(structuredClone(data.rows)); setCatalog(data)
    setRevision(data.revision); setRuns(createRunPreview(data.rows, 'fresh')); setSqlResults({})
  }
  async function load() {
    setLoading(true); setError(''); setMessage('')
    try { accept(await api.orchestrationConfiguration()) }
    catch (failure) { setError(failure.message) }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])
  function update(id, field, value) {
    setRows(current => current.map(row => row.id === id ? { ...row, [field]: value, ...(field === 'parentId' ? {
      ...(row.issue ? (row.parentRules?.[value] || { trigger: null, success: null, warning: null, error: null }) : {}),
      issue: '', resolvedConflict: Boolean(row.issue) || row.resolvedConflict
    } : {}) } : row))
    setMessage('')
  }
  async function save() {
    if (errors.length) return
    setSaving(true); setError(''); setMessage('')
    try {
      const data = await api.saveOrchestrationConfiguration({ rows, revision })
      accept(data); setMessage('Dependency configuration saved to Snowflake.')
    } catch (failure) { setError(failure.message) }
    finally { setSaving(false) }
  }
  function remove(row) { setRows(current => current.filter(item => item.id !== row.id)); setDialog(null); setMessage('') }
  return <section className="page od-page">
    <PageHeader breadcrumb="Orchestration / Dependencies" title="Dependency configuration" subtitle="Maintain workflow parents, job relationships and the conditions that allow execution." actions={<div className="od-actions"><button className="button" type="button" disabled={loading || saving || dirty} onClick={load}>↻ Refresh</button><button className="button primary" type="button" disabled={loading || saving || !dirty || errors.length > 0} onClick={save}>{saving ? 'Saving…' : 'Save changes'}</button></div>} />
    {error && <div className="alert error" role="alert">{error}</div>}
    {loading && !catalog && <LoadingState>Loading workflow and job configuration…</LoadingState>}
    {catalog && <>
      <div className="od-notice"><strong>{catalog.source}</strong><span>Workflow and Job definitions · Dependency rules save to Snowflake · Run preview remains simulated</span></div>
      <div className="od-topline"><div className="od-tabs" role="group" aria-label="Dependency view"><button type="button" aria-pressed={view === 'configuration'} className={view === 'configuration' ? 'active' : ''} onClick={() => setView('configuration')}>Configuration</button><button type="button" aria-pressed={view === 'preview'} className={view === 'preview' ? 'active' : ''} onClick={() => setView('preview')}>Run preview</button></div><span>{workflowCount} workflows · {rows.length - workflowCount} job assignments · <strong>{dirty ? 'Unsaved changes' : 'No unsaved changes'}</strong></span></div>
      {message && <div className="od-message" role="status">{message}</div>}
      {errors.length > 0 && <div className="od-errors" role="alert"><strong>Configuration needs attention</strong><ul>{errors.map((item, index) => <li key={index}>{item.message}</li>)}</ul></div>}
      <fieldset className="od-editor" disabled={saving || loading}>
      {view === 'configuration' ? <>
        <div className="od-grid-heading"><input className="search-input" type="search" aria-label="Search dependency configuration" placeholder="Search workflows or jobs…" value={search} onChange={event => setSearch(event.target.value)} /><div className="od-actions"><button className="button" type="button" disabled={dirty} onClick={() => onNavigate('orchestrationWorkflow')}>Workflow catalog</button><button className="button" type="button" onClick={() => setDialog({ type: 'job' })}>+ Job assignment</button></div></div>
        <p className="od-help">Status rules: Accept = TRUE · Reject = FALSE · Ignore = NULL. Existing relationships with multiple entry jobs or parents must be resolved before saving.</p>
        <ConfigurationGrid rows={visible} allRows={rows} errors={errors} onUpdate={update} onSql={row => setDialog({ type: 'sql', row })} onRemove={row => setDialog({ type: 'remove', row })} />
        {!visible.length && <div className="soft-empty">No workflows match your search.</div>}
        <div className="od-bottom"><p className="od-help">Each workflow has one entry job. Other jobs reference a job within the same workflow. Workflow and job definitions are maintained in their catalogs.</p><button className="button" type="button" disabled={!dirty} onClick={() => { setRows(structuredClone(saved)); setMessage('Draft changes discarded.') }}>Discard changes</button></div>
      </> : dirty || errors.length ? <div className="od-preview-placeholder"><h2>Save your configuration to preview it</h2><p>Resolve any errors and save. The preview uses saved rules and separate simulated run records.</p><button className="button" type="button" onClick={() => setView('configuration')}>Back to configuration</button></div> : <RunPreview rows={saved} runs={runs} setRuns={setRuns} sqlResults={sqlResults} setSqlResults={setSqlResults} />}
      </fieldset>
    </>}
    {dialog?.type === 'job' && <AddDialog rows={rows} jobs={catalog?.jobs || []} onClose={() => setDialog(null)} onAdd={additions => { setRows(current => [...current, ...additions]); setDialog(null); setMessage('') }} />}
    {dialog?.type === 'sql' && <SqlDialog row={dialog.row} rows={rows} onClose={() => setDialog(null)} onSave={sql => { update(dialog.row.id, 'sql', sql); setDialog(null) }} />}
    {dialog?.type === 'remove' && <RemovalDialog row={dialog.row} rows={rows} onClose={() => setDialog(null)} onRemove={() => remove(dialog.row)} />}
  </section>
}
