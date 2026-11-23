import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'
import { configurationApi } from '../api.js'
import ConfigurationSource, { useConfigurationSource } from '../components/ConfigurationSource.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import HierarchyToggle from '../components/HierarchyToggle.jsx'
import { addWorkflowParent, createRunPreview, evaluateDependency, parentOptions, removeWorkflowParent, validateConfiguration } from '../utils/orchestrationDependencies.js'
import './OrchestrationMonitor.css'
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

function ParentDialog({ trial, workflow, rows, onClose, onAdd }) {
  const hasParent = rows.some(row => row.workflowId === workflow.workflowId && row.parentId)
  const options = parentOptions(workflow, rows).filter(parent => !rows.some(row => row.workflowId === workflow.workflowId && row.parentId === parent.id))
  const [draft, setDraft] = useState({ ...workflow, parentId: '', trigger: 'DEP', success: true, warning: false, error: false,
    parentMustBeNewer: true, sql: hasParent ? '' : workflow.sql, allow: hasParent ? true : workflow.allow })
  const update = (_id, field, value) => setDraft(current => ({ ...current, [field]: value }))
  return <Dialog title={`Add parent · ${workflow.name}`} onClose={onClose}>
    <form onSubmit={event => { event.preventDefault(); if (options.some(parent => parent.id === draft.parentId)) onAdd({ ...draft, sql: draft.sql.trim() }) }}>
      <p className="od-help">{hasParent ? 'Every parent must pass its conditions. Adding another parent uses DEP for every parent rule.' : 'Choose the parent workflow and the conditions that allow this workflow to run.'}</p>
      <label htmlFor="od-parent-workflow">Parent workflow</label><select id="od-parent-workflow" required value={draft.parentId} onChange={event => update(draft.id, 'parentId', event.target.value)}><option value="">Choose a parent…</option>{options.map(parent => <option key={parent.id} value={parent.id}>{parent.name}</option>)}</select>
      <label htmlFor="od-parent-trigger">Trigger</label><select id="od-parent-trigger" value={draft.trigger || ''} disabled={trial || hasParent} onChange={event => update(draft.id, 'trigger', event.target.value || null)}><option value="DEP">DEP</option><option value="RUNNER">RUNNER · 30s</option><option value="">External (NULL)</option></select>
      <div className="od-rule-fields">{outcomeFields.map(field => <label key={field}>{field[0].toUpperCase() + field.slice(1)}<RuleSelect row={draft} field={field} rows={rows} onUpdate={update} /></label>)}</div>
      <p className="od-help">Accept allows that parent outcome. Reject blocks it. Ignore applies no restriction.</p>
      <label className="od-check"><input type="checkbox" checked={draft.parentMustBeNewer} onChange={event => update(draft.id, 'parentMustBeNewer', event.target.checked)} />Parent must be newer than the child's last start</label>
      <label>SQL condition<textarea rows={4} disabled={trial} value={draft.sql} onChange={event => update(draft.id, 'sql', event.target.value)} placeholder="Optional · SELECT TRUE" spellCheck={false} /></label>
      <p className="od-help">Optional SQL is checked after the parent rules pass and must return one Boolean value.</p>
      <label htmlFor="od-parent-allow">Allow</label><select id="od-parent-allow" value={String(draft.allow)} onChange={event => update(draft.id, 'allow', event.target.value === 'true')}><option value="true">TRUE · Go</option><option value="false">FALSE · Skip</option></select>
      <div className="od-dialog-actions"><button className="button" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={!draft.parentId}>Add parent</button></div>
    </form>
  </Dialog>
}

function ConfigurationGrid({ trial, groups, allRows, errors, expanded, onToggle, onUpdate, onSql, onAdd, onRemove }) {
  return <div className="od-table-wrap"><table className="od-config-table"><caption className="visually-hidden">Editable workflow dependency configuration</caption><colgroup>{[300, 145, 95, 95, 95, 155, 190, 120, 100].map((width, index) => <col key={index} style={{ width }} />)}</colgroup><thead><tr>{['Workflow / Parent', 'Trigger', 'Success', 'Warning', 'Error', 'Parent must be newer', 'SQL', 'Allow', 'Actions'].map(name => <th scope="col" key={name}>{name}</th>)}</tr></thead>
    {groups.map(({ workflow, rules }) => {
      const parents = rules.filter(row => row.parentId)
      const bodyId = `od-parents-${workflow.workflowId}`
      const canAdd = parentOptions(workflow, allRows).some(parent => !parents.some(row => row.parentId === parent.id))
      return <Fragment key={workflow.workflowId}>
        <tbody className="od-workflow-header"><tr><th scope="rowgroup" colSpan={9}><div className="om-name">
          <HierarchyToggle expanded={expanded.has(workflow.workflowId)} aria-controls={bodyId} aria-label={`${expanded.has(workflow.workflowId) ? 'Hide' : 'Show'} parents for ${workflow.name}`} onClick={() => onToggle(workflow.workflowId)} />
          <div className="om-name-text"><strong>{workflow.name}</strong><small>{parents.length ? `${parents.length} parent${parents.length === 1 ? '' : 's'} · All parent conditions must pass` : trial ? 'No parents · Full graph execution' : 'No parents · External trigger'}</small></div>
          <button className="button od-add-parent" type="button" aria-label={`Add parent for ${workflow.name}`} disabled={!canAdd} title={canAdd ? 'Add a parent and its conditions' : 'All other workflows are already parents'} onClick={() => onAdd(workflow)}>+ Add parent</button>
        </div></th></tr></tbody>
        <tbody id={bodyId} className="od-parent-rules" hidden={!expanded.has(workflow.workflowId)}>{rules.map(row => <tr key={row.id} className={errors.some(error => error.rowId === row.id) ? 'od-invalid' : undefined}>
          <th scope="row"><div className="od-parent-name">{row.parentId ? <select aria-label={`${row.name} parent`} value={row.parentId} onChange={event => onUpdate(row.id, 'parentId', event.target.value)}>{parentOptions(row, allRows).filter(parent => parent.id === row.parentId || !parents.some(rule => rule.parentId === parent.id)).map(parent => <option key={parent.id} value={parent.id}>{parent.name}</option>)}</select> : <span className="od-dim">No parent dependencies</span>}</div></th>
          <td><select disabled={trial || !row.parentId || parents.length > 1} aria-label={`${row.name} trigger`} value={row.trigger || ''} onChange={event => onUpdate(row.id, 'trigger', event.target.value || null)}><option value="">{trial ? 'Full graph' : 'External (NULL)'}</option><option value="DEP">DEP</option><option value="RUNNER">RUNNER · 30s</option></select></td>
          {outcomeFields.map(field => <td key={field}><fieldset className="od-rule-control" disabled={!row.parentId}><RuleSelect row={row} rows={allRows} field={field} onUpdate={onUpdate} /></fieldset></td>)}
          <td><input type="checkbox" aria-label={`${row.name} parent must be newer${row.parentId ? ` · ${allRows.find(item => item.id === row.parentId)?.name}` : ''}`} checked={Boolean(row.parentMustBeNewer)} disabled={!row.parentId} onChange={event => onUpdate(row.id, 'parentMustBeNewer', event.target.checked)} /></td>
          <td><button className={`od-sql-button ${row.sql ? 'has-sql' : ''}`} type="button" disabled={trial} title={row.sql || 'Add a SQL condition'} aria-label={`Edit SQL for ${row.name}${row.parentId ? ` parent ${allRows.find(item => item.id === row.parentId)?.name}` : ''}`} onClick={() => onSql(row)}>{row.sql || '+ Condition'}</button></td>
          <td><select aria-label={`${row.name} allow`} value={String(row.allow)} onChange={event => onUpdate(row.id, 'allow', event.target.value === 'true')}><option value="true">TRUE · Go</option><option value="false">FALSE · Skip</option></select></td>
          <td>{row.parentId && <button className="od-remove" type="button" aria-label={`Remove ${allRows.find(item => item.id === row.parentId)?.name} parent from ${row.name}`} onClick={() => onRemove(row.id)}>Remove</button>}</td>
        </tr>)}</tbody>
      </Fragment>
    })}
  </table></div>
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
  const selection = useConfigurationSource()
  const { source, trial } = selection
  const configurationApiClient = configurationApi(source)
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
  const [expanded, setExpanded] = useState(new Set())
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
    return workflows.filter(row => row.id === row.workflowId && row.name.toLowerCase().includes(query))
      .map(workflow => ({ workflow, rules: workflows.filter(row => row.workflowId === workflow.workflowId) }))
  }, [workflows, search])
  function toggle(workflowId) {
    setExpanded(current => { const next = new Set(current); next.has(workflowId) ? next.delete(workflowId) : next.add(workflowId); return next })
  }
  function accept(data) {
    setRows(data.rows); setSaved(structuredClone(data.rows)); setCatalog(data)
    setRevision(data.revision); setRuns(createRunPreview(data.rows, 'fresh')); setSqlResults({})
    if (workflowName) setExpanded(current => new Set([...current, ...data.rows.filter(row => !row.jobId && row.name === workflowName).map(row => row.workflowId)]))
  }
  async function load() {
    setLoading(true); setError(''); setMessage('')
    try { accept(await configurationApiClient.orchestrationConfiguration()) }
    catch (failure) { setError(failure.message) }
    finally { setLoading(false) }
  }
  useEffect(() => {
    if (!source) return
    setCatalog(null); setRows([]); setSaved([]); setDialog(null); load()
  }, [source])
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
      const data = await configurationApiClient.saveOrchestrationConfiguration({ rows, revision })
      accept(data); setMessage(`Dependency configuration saved to ${trial ? 'PostgreSQL' : 'Snowflake'}.`)
    } catch (failure) { setError(failure.message) }
    finally { setSaving(false) }
  }
  return <section className="page od-page">
    <PageHeader breadcrumb="Workflow / Dependencies" title="Dependency configuration" subtitle="Add parent workflows and maintain the conditions that allow execution." actions={<div className="od-actions">{returnContext && <button className="button" type="button" disabled={saving || dirty} onClick={() => onNavigate('orchestrationMonitor', returnContext)}>← Monitor</button>}<button className="button" type="button" disabled={loading || saving || dirty} onClick={load}>↻ Refresh</button><button className="button primary" type="button" disabled={loading || saving || !dirty || errors.length > 0} onClick={save}>{saving ? 'Saving…' : 'Save changes'}</button></div>} />
    <ConfigurationSource selection={selection} disabled={loading || saving || dirty || !!dialog} revision={revision} revisions={catalog?.revisions} />
    {error && <div className="alert error" role="alert">{error}</div>}
    {loading && !catalog && <LoadingState>Loading workflow dependency configuration…</LoadingState>}
    {catalog && <>
      <div className="od-notice"><strong>{catalog.source}</strong><span>Workflow dependency rules · Changes apply when saved</span></div>
      <div className="od-topline"><div className="od-tabs" role="group" aria-label="Dependency view"><button type="button" aria-pressed={view === 'configuration'} className={view === 'configuration' ? 'active' : ''} onClick={() => setView('configuration')}>Configuration</button><button type="button" aria-pressed={view === 'preview'} className={view === 'preview' ? 'active' : ''} onClick={() => setView('preview')}>Run preview</button></div><span>{new Set(workflows.map(row => row.workflowId)).size} workflows · <strong>{dirty ? 'Unsaved changes' : 'No unsaved changes'}</strong></span></div>
      {message && <div className="od-message" role="status">{message}</div>}
      {errors.length > 0 && <div className="od-errors" role="alert"><strong>Configuration needs attention</strong><ul>{errors.map((item, index) => <li key={index}>{item.message}</li>)}</ul></div>}
      <fieldset className="od-editor" disabled={saving || loading}>
      {view === 'configuration' ? <>
        <div className="od-grid-heading"><input className="search-input" type="search" aria-label="Search dependency configuration" placeholder="Search workflows…" value={search} onChange={event => setSearch(event.target.value)} /><div className="od-actions"><button className="button" type="button" disabled={dirty} onClick={() => onNavigate('orchestrationWorkflow')}>Workflow catalog</button></div></div>
        <p className="od-help">Status rules: Accept = TRUE · Reject = FALSE · Ignore = NULL. Parent must be newer requires the parent to finish strictly after the child's last start; the first run has no freshness cutoff. Expand a workflow to edit its parents. Add or remove parents, then save your changes.</p>
        <div className="od-list-heading"><span>Workflow / Parent hierarchy · {visible.length} workflows</span><div className="od-actions"><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(current => new Set([...current, ...visible.map(group => group.workflow.workflowId)]))}>Expand all</button><button className="button" type="button" disabled={!visible.length} onClick={() => setExpanded(new Set())}>Collapse all</button></div></div>
        <ConfigurationGrid trial={trial} groups={visible} allRows={workflows} errors={errors} expanded={expanded} onToggle={toggle} onUpdate={update} onSql={row => setDialog({ type: 'sql', row })} onAdd={workflow => setDialog({ type: 'parent', workflow })} onRemove={id => { setRows(current => removeWorkflowParent(current, id)); setMessage('Parent removed from draft. Save changes to apply.') }} />
        {!visible.length && <div className="soft-empty">No workflows match your search.</div>}
        <div className="od-bottom"><p className="od-help">Dependencies on this page apply between workflows. Manage jobs and their order on the Workflow page.</p><button className="button" type="button" disabled={!dirty} onClick={() => { setRows(structuredClone(saved)); setError(''); setMessage('Draft changes discarded.') }}>Discard changes</button></div>
      </> : dirty || errors.length ? <div className="od-preview-placeholder"><h2>Save your configuration to preview it</h2><p>Resolve any errors and save. The preview uses saved rules and separate simulated run records.</p><button className="button" type="button" onClick={() => setView('configuration')}>Back to configuration</button></div> : <RunPreview rows={savedWorkflows} runs={runs} setRuns={setRuns} sqlResults={sqlResults} setSqlResults={setSqlResults} />}
      </fieldset>
    </>}
    {dialog?.type === 'parent' && <ParentDialog trial={trial} workflow={dialog.workflow} rows={workflows} onClose={() => setDialog(null)} onAdd={values => { setRows(current => addWorkflowParent(current, dialog.workflow.workflowId, values)); setExpanded(current => new Set([...current, dialog.workflow.workflowId])); setMessage('Parent added to draft. Save changes to apply.'); setDialog(null) }} />}
    {dialog?.type === 'sql' && <SqlDialog row={dialog.row} rows={rows} onClose={() => setDialog(null)} onSave={sql => { update(dialog.row.id, 'sql', sql); setDialog(null) }} />}
  </section>
}
