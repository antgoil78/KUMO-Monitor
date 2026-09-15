import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'

const booleanFields = [['success', 'Success'], ['warning', 'Warning'], ['error', 'Error'], ['newer', 'Newer']]

function workflowLabel(workflow) {
  return [workflow.workflowGroup, workflow.workflowName].filter(Boolean).join(' / ') || workflow.workflowId
}

function normalizeRule(rule, index) {
  return { ...rule, dependeeType: String(rule.dependeeType || 'WORKFLOW').toUpperCase(), _key: `${rule.workflowId}-${rule.dependeeId}-${index}`, _originalDependeeId: rule.dependeeId, _new: false }
}

export default function Dependencies() {
  const [workflows, setWorkflows] = useState([])
  const [models, setModels] = useState([])
  const [rows, setRows] = useState([])
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('')
  const [workflowSearch, setWorkflowSearch] = useState('')
  const [workflowPickerOpen, setWorkflowPickerOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [deleteCandidate, setDeleteCandidate] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const labels = useMemo(() => new Map(workflows.map(workflow => [workflowLabel(workflow), workflow])), [workflows])
  const filteredWorkflows = useMemo(() => {
    const query = workflowSearch.trim().toLowerCase()
    if (!query || labels.has(workflowSearch)) return workflows
    return workflows.filter(workflow => `${workflowLabel(workflow)} ${workflow.workflowId}`.toLowerCase().includes(query))
  }, [labels, workflowSearch, workflows])

  useEffect(() => {
    let cancelled = false
    api.dependencies().then(data => {
      if (cancelled) return
      setWorkflows(data.workflows || [])
      setModels(data.models || [])
    }).catch(err => {
      if (!cancelled) setError(err.message || String(err))
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  }, [])

  async function chooseWorkflow(workflow) {
    if (!workflow) return
    setSelectedWorkflowId(workflow.workflowId)
    setWorkflowSearch(workflowLabel(workflow))
    setWorkflowPickerOpen(false)
    setLoading(true)
    setError('')
    setNotice('')
    try {
      const data = await api.dependencies(workflow.workflowId)
      setWorkflows(data.workflows || workflows)
      setModels(data.models || models)
      setRows((data.rules || []).map(normalizeRule))
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  function updateWorkflowSearch(value) {
    setWorkflowSearch(value)
    setWorkflowPickerOpen(true)
  }

  function patchRow(key, field, value) {
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
      setRows((data.rules || []).map(normalizeRule))
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
      setRows((data.rules || []).map(normalizeRule))
      setNotice('Saved dependency rules reloaded.')
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="page dependencies-page">
      <PageHeader breadcrumb="Workflow / Dependencies" title="Dependencies" subtitle="Maintain workflow dependency rules and their triggering statuses." actions={<button className="button primary" type="button" onClick={addRule} disabled={!selectedWorkflowId || saving}>+ Add rule</button>} />

      <div className="vision-card dependency-workflow-picker">
        <label htmlFor="dependency-workflow-search">Workflow</label>
        <div className="dependency-workflow-combobox">
          <input
            id="dependency-workflow-search"
            className="search-input"
            value={workflowSearch}
            onChange={event => updateWorkflowSearch(event.target.value)}
            onFocus={() => setWorkflowPickerOpen(true)}
            onBlur={() => window.setTimeout(() => setWorkflowPickerOpen(false), 150)}
            onKeyDown={event => {
              if (event.key === 'Enter' && filteredWorkflows[0]) {
                event.preventDefault()
                chooseWorkflow(filteredWorkflows[0])
              }
              if (event.key === 'Escape') setWorkflowPickerOpen(false)
            }}
            placeholder="Search and choose a workflow…"
            autoComplete="off"
            role="combobox"
            aria-expanded={workflowPickerOpen}
            aria-controls="dependency-workflow-options"
          />
          {workflowPickerOpen && <div className="dependency-workflow-options" id="dependency-workflow-options" role="listbox">
            {filteredWorkflows.length ? filteredWorkflows.map(workflow => (
              <button key={workflow.workflowId} type="button" role="option" aria-selected={workflow.workflowId === selectedWorkflowId} onMouseDown={event => event.preventDefault()} onClick={() => chooseWorkflow(workflow)}>
                <strong>{workflow.workflowName}</strong>
                <span>{workflow.workflowGroup || 'Ungrouped'} · {workflow.workflowId}</span>
              </button>
            )) : <div className="dependency-workflow-no-results">No workflows match your search.</div>}
          </div>}
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {notice && <div className="alert info">{notice}</div>}

      <div className="vision-card dependency-rule-card">
        {!selectedWorkflowId ? <div className="empty-state">Choose a workflow to view and edit its dependency rules.</div> : loading ? <div className="empty-state">Loading dependency rules…</div> : rows.length === 0 ? <div className="empty-state">No dependency rules found. Click Add rule to add one.</div> : (<>
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
            <button className="button dependency-cancel-button" type="button" disabled={saving} onClick={cancelChanges}>Cancel</button>
            <button className="button dependency-save-button" type="button" disabled={saving} onClick={saveAll}>{saving ? 'Saving…' : 'Save'}</button>
          </div>
        </>)}
      </div>

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
