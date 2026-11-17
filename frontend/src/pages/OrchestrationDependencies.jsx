import { useEffect, useMemo, useRef, useState } from 'react'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'
import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { createRunPreview, evaluateDependency, parentOptions, validateConfiguration } from '../utils/orchestrationDependencies.js'
import './OrchestrationDependencies.css'

const outcomeFields = ['success', 'warning', 'error']
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

function SqlDialog({ row, rows, onClose, onSave }) {
  const [sql, setSql] = useState(row.sql)
  return <Dialog title={`SQL condition · ${objectLabel(row, rows)}`} onClose={onClose}><form onSubmit={event => { event.preventDefault(); onSave(sql.trim()) }}><p className="od-help">This condition is evaluated after the parent status rules pass. It must return one Boolean value.</p><label>SQL condition<textarea rows={8} value={sql} onChange={event => setSql(event.target.value)} placeholder="SELECT TRUE" spellCheck={false} /></label><p className="od-help">SQL is stored as draft text here. Use a simulated result in Run preview to explore its effect.</p><div className="od-dialog-actions"><button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit">Apply condition</button></div></form></Dialog>
}

function RuleSelect({ row, field, rows, onUpdate }) {
  return <select className={row[field] === true ? 'od-accept' : row[field] === false ? 'od-reject' : ''} aria-label={`${objectLabel(row, rows)} ${field} rule`} value={row[field] === null ? '' : String(row[field])} onChange={event => onUpdate(row.id, field, event.target.value === '' ? null : event.target.value === 'true')}><option value="">Ignore</option><option value="true">Accept</option><option value="false">Reject</option></select>
}

function ConfigurationGrid({ rows, allRows, errors, onUpdate, onSql }) {
  return <div className="od-table-wrap"><table className="od-config-table"><caption className="visually-hidden">Editable workflow dependency configuration</caption><colgroup>{[220, 220, 150, 105, 105, 105, 150, 200, 130].map((width, index) => <col key={index} style={{ width }} />)}</colgroup><thead><tr>{['Workflow', 'Parent', 'Trigger', 'Success', 'Warning', 'Error', 'Parent must be newer', 'SQL', 'Allow'].map(name => <th scope="col" key={name}>{name}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.id} className={`od-workflow ${errors.some(error => error.rowId === row.id) ? 'od-invalid' : ''}`}>
      <th scope="row"><strong>{row.name}</strong></th>
      <td><select aria-label={`${row.name} parent`} value={row.issue ? '__unresolved__' : row.parentId || ''} onChange={event => onUpdate(row.id, 'parentId', event.target.value || null)}>{row.issue && <option value="__unresolved__" disabled>Choose one parent…</option>}<option value="">No parent</option>{parentOptions(row, allRows).map(parent => <option key={parent.id} value={parent.id}>{parent.name}</option>)}</select></td>
      <td><select aria-label={`${row.name} trigger`} value={row.trigger || ''} onChange={event => onUpdate(row.id, 'trigger', event.target.value || null)}><option value="">External (NULL)</option><option value="DEP">DEP</option><option value="RUNNER">RUNNER · 30s</option></select></td>
      {outcomeFields.map(field => <td key={field}><RuleSelect row={row} rows={allRows} field={field} onUpdate={onUpdate} /></td>)}
      <td><input type="checkbox" aria-label={`${row.name} parent must be newer${row.parentId ? ` · ${allRows.find(item => item.id === row.parentId)?.name}` : ''}`} checked={Boolean(row.parentMustBeNewer)} disabled={!row.parentId} onChange={event => onUpdate(row.id, 'parentMustBeNewer', event.target.checked)} /></td>
      <td><button className={`od-sql-button ${row.sql ? 'has-sql' : ''}`} type="button" title={row.sql || 'Add a SQL condition'} aria-label={`Edit SQL for ${row.name}`} onClick={() => onSql(row)}>{row.sql || '+ Condition'}</button></td>
      <td><select aria-label={`${row.name} allow`} value={String(row.allow)} onChange={event => onUpdate(row.id, 'allow', event.target.value === 'true')}><option value="true">TRUE · Go</option><option value="false">FALSE · Skip</option></select></td>
    </tr>)}</tbody></table></div>
}

function RunPreview({ rows, runs, setRuns, sqlResults, setSqlResults }) {
  function reset() { setRuns(createRunPreview(rows, 'fresh')); setSqlResults({}) }
  return <div className="od-preview">
    <div className="od-preview-heading"><div><h2>Run preview</h2><p>Change workflow run states to see eligibility. Each workflow has a separate execution record within this preview cycle.</p></div><div className="od-actions"><button className="button" type="button" onClick={reset}>New preview cycle</button></div></div>
    <div className="od-preview-note">Workflow dependencies wait for a completed parent workflow. Ignore applies no restriction to that status. Changes here simulate outcomes; nothing is dispatched.</div>
    <div className="od-table-wrap"><table className="od-run-table"><caption className="visually-hidden">Simulated execution states and evaluated dependency decisions</caption><thead><tr>{['Object', 'Run status', 'Last start', 'Completion time', 'Parent run', 'SQL result', 'Decision'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{rows.map(row => {
      const runKey = row.workflowId
      const evaluation = evaluateDependency(row, rows, runs, sqlResults)
      const parent = rows.find(item => item.id === row.parentId)
      return <tr key={row.id} className={row.jobId ? 'od-job' : 'od-workflow'}>
        <th scope="row"><strong>{objectLabel(row, rows)}</strong><small title={runs[row.id].runId}>{row.jobId ? 'Job execution' : 'Workflow execution'}</small></th>
        <td><select aria-label={`${objectLabel(row, rows)} run status`} value={runs[runKey].status} onChange={event => setRuns(current => ({ ...current, [runKey]: { ...current[runKey], status: event.target.value, endedAt: ['SUCCESS', 'WARNING', 'ERROR', 'SKIPPED'].includes(event.target.value) ? new Date().toISOString().slice(0, 19) : null } }))}>{['WAITING', 'RUNNING', 'SUCCESS', 'WARNING', 'ERROR', 'SKIPPED'].map(status => <option key={status}>{status}</option>)}</select></td>
        <td><input type="datetime-local" step="1" aria-label={`${row.name} last start`} value={runs[runKey].lastStartedAt || ''} onChange={event => setRuns(current => ({ ...current, [runKey]: { ...current[runKey], lastStartedAt: event.target.value || null } }))} /></td>
        <td><input type="datetime-local" step="1" aria-label={`${row.name} completion time`} value={runs[runKey].endedAt || ''} onChange={event => setRuns(current => ({ ...current, [runKey]: { ...current[runKey], endedAt: event.target.value || null } }))} /></td>
        <td>{parent ? <><span>{objectLabel(parent, rows)}</span><small><StatusBadge status={runs[parent.id]?.status} /></small></> : <span className="od-dim">External trigger</span>}</td>
        <td>{row.sql ? <select aria-label={`${objectLabel(row, rows)} SQL result`} value={sqlResults[row.id] === undefined ? '' : String(sqlResults[row.id])} onChange={event => setSqlResults(current => ({ ...current, [row.id]: event.target.value === '' ? undefined : event.target.value === 'error' ? 'error' : event.target.value === 'true' }))}><option value="">Not evaluated</option><option value="true">TRUE</option><option value="false">FALSE</option><option value="error">SQL error</option></select> : <span className="od-dim">No condition</span>}</td>
        <td><span className={`od-decision od-${evaluation.state.toLowerCase().replace(' ', '-')}`}>{evaluation.state}</span><small>{evaluation.reason}</small></td>
      </tr>
    })}</tbody></table></div>
    <p className="od-help">A new preview cycle clears all simulated workflow outcomes. Run status is not saved with configuration.</p>
  </div>
}

export default function OrchestrationDependencies({ onNavigate, workflowName = '', returnContext = null }) {
  const [rows, setRows] = useState([])
  const [saved, setSaved] = useState([])
  const [catalog, setCatalog] = useState(null)
  const [revision, setRevision] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState(workflowName)
  const [view, setView] = useState('configuration')
  const [dialog, setDialog] = useState(null)
  const [message, setMessage] = useState('')
  const [runs, setRuns] = useState({})
  const [sqlResults, setSqlResults] = useState({})
  const errors = useMemo(() => rows.length ? validateConfiguration(rows) : [], [rows])
  const dirty = JSON.stringify(rows) !== JSON.stringify(saved)
  useEffect(() => {
    if (!dirty) return undefined
    const guard = event => { event.preventDefault(); event.returnValue = '' }
    const navigate = event => {
      if (!window.confirm('Discard unsaved dependency changes and leave this page?')) event.preventDefault()
    }
    window.addEventListener('beforeunload', guard)
    window.addEventListener('kumo:before-navigate', navigate)
    return () => { window.removeEventListener('beforeunload', guard); window.removeEventListener('kumo:before-navigate', navigate) }
  }, [dirty])
  // Retain job assignments in the saved payload; this page edits workflow rules only.
  const workflows = useMemo(() => rows.filter(row => !row.jobId), [rows])
  const savedWorkflows = useMemo(() => saved.filter(row => !row.jobId), [saved])
  const visible = useMemo(() => {
    const query = search.trim().toLowerCase()
    return workflows.filter(row => row.name.toLowerCase().includes(query))
  }, [workflows, search])
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
      parentMustBeNewer: value ? (row.parentId ? row.parentMustBeNewer : true) : false, issue: '', resolvedConflict: Boolean(row.issue) || row.resolvedConflict
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
  return <section className="page od-page">
    <PageHeader breadcrumb="Workflow / Dependencies" title="Dependency configuration" subtitle="Maintain workflow parents and the conditions that allow execution." actions={<div className="od-actions">{returnContext && <button className="button" type="button" disabled={saving || dirty} onClick={() => onNavigate('orchestrationMonitor', returnContext)}>← Monitor</button>}<button className="button" type="button" disabled={loading || saving || dirty} onClick={load}>↻ Refresh</button><button className="button primary" type="button" disabled={loading || saving || !dirty || errors.length > 0} onClick={save}>{saving ? 'Saving…' : 'Save changes'}</button></div>} />
    {error && <div className="alert error" role="alert">{error}</div>}
    {loading && !catalog && <LoadingState>Loading workflow dependency configuration…</LoadingState>}
    {catalog && <>
      <div className="od-notice"><strong>{catalog.source}</strong><span>Workflow dependency rules · Save to Snowflake · Run preview remains simulated</span></div>
      <div className="od-topline"><div className="od-tabs" role="group" aria-label="Dependency view"><button type="button" aria-pressed={view === 'configuration'} className={view === 'configuration' ? 'active' : ''} onClick={() => setView('configuration')}>Configuration</button><button type="button" aria-pressed={view === 'preview'} className={view === 'preview' ? 'active' : ''} onClick={() => setView('preview')}>Run preview</button></div><span>{new Set(workflows.map(row => row.workflowId)).size} workflows · <strong>{dirty ? 'Unsaved changes' : 'No unsaved changes'}</strong></span></div>
      {message && <div className="od-message" role="status">{message}</div>}
      {errors.length > 0 && <div className="od-errors" role="alert"><strong>Configuration needs attention</strong><ul>{errors.map((item, index) => <li key={index}>{item.message}</li>)}</ul></div>}
      <fieldset className="od-editor" disabled={saving || loading}>
      {view === 'configuration' ? <>
        <div className="od-grid-heading"><input className="search-input" type="search" aria-label="Search dependency configuration" placeholder="Search workflows…" value={search} onChange={event => setSearch(event.target.value)} /><div className="od-actions"><button className="button" type="button" disabled={dirty} onClick={() => onNavigate('orchestrationWorkflow')}>Workflow catalog</button></div></div>
        <p className="od-help">Status rules: Accept = TRUE · Reject = FALSE · Ignore = NULL. Parent must be newer requires the parent to finish strictly after the child's last start; the first run has no freshness cutoff. Each row is one parent rule. Resolve any configuration errors before saving.</p>
        <ConfigurationGrid rows={visible} allRows={workflows} errors={errors} onUpdate={update} onSql={row => setDialog({ type: 'sql', row })} />
        {!visible.length && <div className="soft-empty">No workflows match your search.</div>}
        <div className="od-bottom"><p className="od-help">Dependencies on this page apply between workflows. Manage jobs and their order on the Workflow page.</p><button className="button" type="button" disabled={!dirty} onClick={() => { setRows(structuredClone(saved)); setMessage('Draft changes discarded.') }}>Discard changes</button></div>
      </> : dirty || errors.length ? <div className="od-preview-placeholder"><h2>Save your configuration to preview it</h2><p>Resolve any errors and save. The preview uses saved rules and separate simulated run records.</p><button className="button" type="button" onClick={() => setView('configuration')}>Back to configuration</button></div> : <RunPreview rows={savedWorkflows} runs={runs} setRuns={setRuns} sqlResults={sqlResults} setSqlResults={setSqlResults} />}
      </fieldset>
    </>}
    {dialog?.type === 'sql' && <SqlDialog row={dialog.row} rows={rows} onClose={() => setDialog(null)} onSave={sql => { update(dialog.row.id, 'sql', sql); setDialog(null) }} />}
  </section>
}
