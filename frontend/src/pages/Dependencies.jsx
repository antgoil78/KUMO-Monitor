import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'

const booleanFields = [['success', 'Success'], ['warning', 'Warning'], ['error', 'Error'], ['newer', 'Newer']]

function workflowLabel(workflow) {
  return [workflow.workflowGroup, workflow.workflowName].filter(Boolean).join(' / ') || workflow.workflowId
}

function normalizeRule(rule, index) {
  return { ...rule, dependeeType: String(rule.dependeeType || 'WORKFLOW').toUpperCase(), _key: `${rule.workflowId}-${rule.dependeeId}-${index}`, _originalDependeeId: rule.dependeeId, _new: false }
}

function rulesSnapshot(rules) {
  return JSON.stringify(rules.map(row => ({
    workflowId: row.workflowId, dependeeType: row.dependeeType, dependeeId: row.dependeeId,
    success: Boolean(row.success), warning: Boolean(row.warning), error: Boolean(row.error),
    newer: Boolean(row.newer), activeFl: Boolean(row.activeFl)
  })).sort((a, b) => b.dependeeType.localeCompare(a.dependeeType) || a.dependeeId.localeCompare(b.dependeeId)))
}

function displayDate(value) {
  if (!value) return 'Not loaded'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

export default function Dependencies({ workflow, onNavigate }) {
  const [workflows, setWorkflows] = useState([])
  const [models, setModels] = useState([])
  const [rows, setRows] = useState([])
  const [savedRows, setSavedRows] = useState([])
  const selectedWorkflowId = workflow?.workflowId || ''
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [deleteCandidate, setDeleteCandidate] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [verifying, setVerifying] = useState(false)
  const [verification, setVerification] = useState(null)
  const [overviewSearch, setOverviewSearch] = useState('')
  const [overviewSort, setOverviewSort] = useState({ key: 'workflow', direction: 'asc' })

  const hasUnsavedRules = rulesSnapshot(rows) !== rulesSnapshot(savedRows)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    api.dependencies(selectedWorkflowId).then(data => {
      if (cancelled) return
      setWorkflows(data.workflows || [])
      setModels(data.models || [])
      const loadedRules = (data.rules || []).map(normalizeRule)
      setRows(loadedRules)
      setSavedRows(loadedRules)
    }).catch(err => {
      if (!cancelled) setError(err.message || String(err))
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  }, [selectedWorkflowId])

  const workflowById = useMemo(() => new Map(workflows.map(item => [String(item.workflowId), item])), [workflows])
  const overviewRows = useMemo(() => {
    const query = overviewSearch.trim().toLowerCase()
    const valueFor = row => {
      const owner = workflowById.get(String(row.workflowId))
      if (overviewSort.key === 'workflow') return workflowLabel(owner || { workflowId: row.workflowId })
      if (overviewSort.key === 'active') return row.activeFl ? '1' : '0'
      if (overviewSort.key === 'type') return row.dependeeType
      if (overviewSort.key === 'dependee') return row.dependeeId
      return ''
    }
    return rows.filter(row => {
      const owner = workflowById.get(String(row.workflowId))
      return !query || `${workflowLabel(owner || { workflowId: row.workflowId })} ${row.workflowId} ${row.dependeeType} ${row.dependeeId}`.toLowerCase().includes(query)
    }).sort((a, b) => {
      const result = String(valueFor(a) || '').localeCompare(String(valueFor(b) || ''), undefined, { numeric: true, sensitivity: 'base' })
      return overviewSort.direction === 'asc' ? result : -result
    })
  }, [rows, workflowById, overviewSearch, overviewSort])

  function changeOverviewSort(key) {
    setOverviewSort(current => ({ key, direction: current.key === key && current.direction === 'asc' ? 'desc' : 'asc' }))
  }

  function sortLabel(key, label) {
    return <button type="button" className="dependency-sort-button" onClick={() => changeOverviewSort(key)}>{label}<span aria-hidden="true">{overviewSort.key === key ? (overviewSort.direction === 'asc' ? '↑' : '↓') : '↕'}</span></button>
  }

  function patchRow(key, field, value) {
    setVerification(null)
    setRows(current => current.map(row => {
      if (row._key !== key) return row
      if (field === 'dependeeType') {
        return { ...row, dependeeType: value, dependeeId: '' }
      }
      return { ...row, [field]: value }
    }))
  }

  function addRule() {
    if (!selectedWorkflowId) return
    setRows(current => [...current, {
      workflowId: selectedWorkflowId, dependeeType: '', dependeeId: '',
      success: false, warning: false, error: false, newer: false, activeFl: false,
      _key: `new-${Date.now()}`, _originalDependeeId: '', _new: true
    }])
    setVerification(null)
    setNotice('New rule added. Choose its values and click Add rule.')
  }

  async function saveAll() {
    if (rows.some(row => !row.dependeeType || !row.dependeeId)) {
      setError('Choose a dependee type and dependee for every new rule before saving.')
      return
    }
    setSaving(true)
    setError('')
    setNotice('')
    try {
      for (const row of rows) {
        const payload = {
          workflowId: selectedWorkflowId, dependeeType: row.dependeeType, dependeeId: row.dependeeId,
          originalDependeeId: row._originalDependeeId, success: row.success, warning: row.warning,
          error: row.error, newer: row.newer, activeFl: row.activeFl
        }
        if (row._new) await api.createDependencyRule(payload)
        else await api.updateDependencyRule(payload)
        setRows(current => current.map(item => item._key === row._key ? { ...item, _new: false, _originalDependeeId: row.dependeeId } : item))
      }
      const data = await api.dependencies(selectedWorkflowId)
      const loadedRules = (data.rules || []).map(normalizeRule)
      setRows(loadedRules)
      setSavedRows(loadedRules)
      setNotice('Dependency rules saved successfully.')
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setSaving(false)
    }
  }

  async function confirmDelete() {
    const row = deleteCandidate
    if (!row) return
    if (row._new) {
      setRows(current => current.filter(item => item._key !== row._key))
      setDeleteCandidate(null)
      return
    }
    setSaving(true)
    setError('')
    try {
      await api.deleteDependencyRule({ workflowId: selectedWorkflowId, dependeeId: row._originalDependeeId })
      setRows(current => current.filter(item => item._key !== row._key))
      setSavedRows(current => current.filter(item => item._originalDependeeId !== row._originalDependeeId))
      setVerification(null)
      setNotice('Dependency rule deleted.')
      setDeleteCandidate(null)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setSaving(false)
    }
  }

  async function cancelChanges() {
    if (!window.confirm('Discard all unsaved dependency changes and reload the saved rules?')) return
    setLoading(true)
    setError('')
    setNotice('')
    try {
      const data = await api.dependencies(selectedWorkflowId)
      const loadedRules = (data.rules || []).map(normalizeRule)
      setRows(loadedRules)
      setSavedRows(loadedRules)
      setVerification(null)
      setNotice('Saved dependency rules reloaded.')
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  async function verifyRules() {
    if (!selectedWorkflowId || hasUnsavedRules) return
    setVerifying(true)
    setError('')
    setNotice('')
    try {
      setVerification(await api.verifyDependencyRules(selectedWorkflowId))
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setVerifying(false)
    }
  }

  return (
    <div className="page dependencies-page">
      <PageHeader breadcrumb="Workflow / Dependencies" title="Dependencies" subtitle={workflow ? `Maintain dependency rules for ${workflow.workflowName}.` : 'Search and review dependency rules across all workflows.'} actions={workflow ? <div className="page-header-actions"><button className="button" type="button" onClick={() => onNavigate('dependencies')}>← All dependencies</button><button className="button primary" type="button" onClick={addRule} disabled={saving}>+ Add rule</button></div> : null} />

      {workflow && <div className="vision-card dependency-workflow-context"><span>Workflow</span><strong>{workflow.workflowName}</strong><small>{workflow.workflowGroup || 'Ungrouped'} · {workflow.workflowId}</small></div>}

      {error && <div className="alert error">{error}</div>}
      {notice && <div className="alert info">{notice}</div>}

      {!selectedWorkflowId && <div className="dependency-overview-toolbar"><input className="search-input" value={overviewSearch} onChange={event => setOverviewSearch(event.target.value)} placeholder="Search workflows, dependees or types…" aria-label="Search dependency rules" /><span>{overviewRows.length} rule{overviewRows.length === 1 ? '' : 's'}</span></div>}

      <div className="vision-card dependency-rule-card">
        {loading ? <LoadingState>Loading dependency rules…</LoadingState> : !selectedWorkflowId ? (
          overviewRows.length ? <div className="dependency-table-scroll"><table className="dependency-rule-table dependency-overview-table">
            <thead><tr><th>{sortLabel('workflow', 'Workflow')}</th><th>{sortLabel('active', 'Active')}</th><th>{sortLabel('type', 'Dependee type')}</th><th>{sortLabel('dependee', 'Dependee')}</th><th>Accepted status</th><th>Newer</th><th /></tr></thead>
            <tbody>{overviewRows.map(row => {
              const owner = workflowById.get(String(row.workflowId))
              const accepted = booleanFields.slice(0, 3).filter(([field]) => row[field]).map(([, label]) => label)
              return <tr key={row._key}>
                <td><strong>{owner?.workflowName || row.workflowId}</strong><small>{owner?.workflowGroup || 'Ungrouped'}</small></td>
                <td><span className={`dependency-state ${row.activeFl ? 'active' : 'inactive'}`}>{row.activeFl ? 'Active' : 'Inactive'}</span></td>
                <td><span className="type-chip">{row.dependeeType}</span></td>
                <td><code>{row.dependeeId}</code></td>
                <td>{accepted.length ? accepted.join(', ') : '—'}</td>
                <td>{row.newer ? 'Yes' : 'No'}</td>
                <td><button className="small-button" type="button" onClick={() => onNavigate('dependencies', { workflow: owner || { workflowId: row.workflowId, workflowName: row.workflowId } })}>Edit</button></td>
              </tr>
            })}</tbody>
          </table></div> : <div className="empty-state">{rows.length ? 'No dependency rules match your search.' : 'No dependency rules are configured.'}</div>
        ) : rows.length === 0 ? <div className="empty-state">No dependency rules found. Click Add rule to add one.</div> : (<>
          <div className="dependency-table-scroll">
            <table className="dependency-rule-table">
              <thead><tr><th className="boolean-column active-column">Active</th><th>Dependee type</th><th>Dependee</th>{booleanFields.map(([, label]) => <th className="boolean-column" key={label}>{label}</th>)}<th className="dependency-actions-column" /></tr></thead>
              <tbody>{rows.map(row => {
                const choices = row.dependeeType === 'MODEL' ? models.map(model => ({ id: model, label: model })) : row.dependeeType === 'WORKFLOW' ? workflows.map(workflow => ({ id: workflow.workflowId, label: workflowLabel(workflow) })) : []
                return <tr key={row._key} className={row._new ? 'dependency-new-row' : ''}>
                  <td className="boolean-column active-column"><input type="checkbox" aria-label="Active" checked={Boolean(row.activeFl)} onChange={event => patchRow(row._key, 'activeFl', event.target.checked)} /></td>
                  <td><select value={row.dependeeType} onChange={event => patchRow(row._key, 'dependeeType', event.target.value)}><option value="" disabled>Choose type…</option><option value="WORKFLOW">Workflow</option><option value="MODEL">Model</option></select></td>
                  <td><select value={row.dependeeId || ''} disabled={!row.dependeeType} onChange={event => patchRow(row._key, 'dependeeId', event.target.value)}><option value="">{row.dependeeType ? 'Choose…' : 'Choose type first…'}</option>{choices.map(choice => <option key={choice.id} value={choice.id}>{choice.label}</option>)}</select></td>
                  {booleanFields.map(([field, label]) => <td className="boolean-column" key={field}><input type="checkbox" aria-label={label} checked={Boolean(row[field])} onChange={event => patchRow(row._key, field, event.target.checked)} /></td>)}
                  <td className="dependency-actions-column"><button className="small-button dependency-delete-button" type="button" disabled={saving} onClick={() => setDeleteCandidate(row)}>Delete</button></td>
                </tr>
              })}</tbody>
            </table>
          </div>
          <div className="dependency-save-bar">
            <button className="button dependency-verify-button" type="button" onClick={verifyRules} disabled={!selectedWorkflowId || hasUnsavedRules || saving || verifying} title={hasUnsavedRules ? 'Save or cancel changes before verifying' : 'Evaluate the saved ruleset'}>{verifying ? 'Verifying…' : 'Verify result'}</button>
            <div className="dependency-save-actions">
              <button className="button dependency-cancel-button" type="button" disabled={saving} onClick={cancelChanges}>Cancel</button>
              <button className="button dependency-save-button" type="button" disabled={saving} onClick={saveAll}>{saving ? 'Saving…' : 'Save'}</button>
            </div>
          </div>
        </>)}
      </div>

      {verification && <div className={`vision-card dependency-verification ${verification.resultRuleset ? 'passed' : 'failed'}`}>
        <div className="dependency-verification-summary">
          <span className="dependency-verification-icon" aria-hidden="true">{verification.resultRuleset ? '✓' : '!'}</span>
          <div><span>Verification result</span><h2>{verification.resultRuleset ? 'Ruleset passed' : 'Ruleset failed'}</h2><p>{verification.rows?.length || 0} active dependenc{verification.rows?.length === 1 ? 'y' : 'ies'} evaluated.</p></div>
        </div>
        {verification.rows?.length ? <div className="dependency-verification-grid">{verification.rows.map((result, index) => (
          <article className={`${result.resultStatus && result.resultMustBeNewer ? 'passed' : 'failed'}`} key={`${result.dependeeType}-${result.dependeeName}-${index}`}>
            <div className="dependency-verification-heading"><span>{result.dependeeType}</span><strong>{result.dependeeName}</strong><b>{result.resultStatus && result.resultMustBeNewer ? 'Pass' : 'Fail'}</b></div>
            <dl>
              <div><dt>Current status</dt><dd>{result.dependencyStatus || 'No status'}</dd></div>
              <div><dt>Accepted</dt><dd>{String(result.acceptedStatus || '').split('|').filter(Boolean).join(', ') || 'None'}</dd></div>
              <div><dt>Status match</dt><dd className={result.resultStatus ? 'pass' : 'fail'}>{result.resultStatus ? 'Yes' : 'No'}</dd></div>
              <div><dt>Must be newer</dt><dd>{result.mustBeNewer ? 'Yes' : 'No'}</dd></div>
              <div><dt>Freshness check</dt><dd className={result.resultMustBeNewer ? 'pass' : 'fail'}>{result.resultMustBeNewer ? 'Pass' : 'Fail'}</dd></div>
              <div><dt>Dependee loaded</dt><dd>{displayDate(result.dependencyLoadedDttm)}</dd></div>
              <div><dt>Workflow loaded</dt><dd>{displayDate(result.loadedDttm)}</dd></div>
              <div><dt>Run ID</dt><dd title={result.runId || ''}>{result.runId || '—'}</dd></div>
            </dl>
          </article>
        ))}</div> : <div className="empty-state">The procedure returned no active dependency rows.</div>}
      </div>}

      {deleteCandidate && <div className="dependency-confirm-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget && !saving) setDeleteCandidate(null) }}>
        <div className="dependency-confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="dependency-delete-title" aria-describedby="dependency-delete-description">
          <div className="dependency-confirm-icon" aria-hidden="true">×</div>
          <div className="dependency-confirm-copy">
            <span>Delete dependency rule</span>
            <h2 id="dependency-delete-title">Are you sure?</h2>
            <p id="dependency-delete-description">The rule for <strong>{deleteCandidate.dependeeId || 'this dependee'}</strong> will be removed. This action cannot be undone.</p>
          </div>
          <div className="dependency-confirm-actions">
            <button className="button dependency-cancel-button" type="button" disabled={saving} onClick={() => setDeleteCandidate(null)}>Cancel</button>
            <button className="button dependency-confirm-delete" type="button" disabled={saving} onClick={confirmDelete}>{saving ? 'Deleting…' : 'Delete rule'}</button>
          </div>
        </div>
      </div>}
    </div>
  )
}
