import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'

const groups = [
  { key: 'ENVIRONMENT', label: 'Environment variables' },
  { key: 'SYSTEM', label: 'System settings' }
]
const valueTypes = ['STRING', 'INTEGER', 'BOOLEAN', 'JSON']

function normalize(item, index) {
  return { ...item, _key: `${item.parameterGroup}-${item.parameterKey}-${index}`, _originalGroup: item.parameterGroup, _originalKey: item.parameterKey, _new: false }
}

function clean(item) {
  return JSON.stringify({ group: item.parameterGroup, key: item.parameterKey, value: item.parameterValue, type: item.valueType, description: item.description, secret: Boolean(item.isSecret), active: Boolean(item.activeFl) })
}

export default function EnvironmentSettings({ embedded = false }) {
  const [activeGroup, setActiveGroup] = useState('ENVIRONMENT')
  const [rows, setRows] = useState([])
  const [savedRows, setSavedRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [deleteCandidate, setDeleteCandidate] = useState(null)
  const [visibleSecrets, setVisibleSecrets] = useState(new Set())

  const visibleRows = useMemo(() => rows.filter(row => row.parameterGroup === activeGroup), [activeGroup, rows])
  const hasChanges = rows.some(row => row._new || clean(row) !== clean(savedRows.find(saved => saved._originalGroup === row._originalGroup && saved._originalKey === row._originalKey) || {}))

  async function load(message = '') {
    setLoading(true)
    setError('')
    try {
      const data = await api.applicationParameters()
      const loaded = (data.parameters || []).map(normalize)
      setRows(loaded)
      setSavedRows(loaded)
      setNotice(message)
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  function patch(key, field, value) {
    setRows(current => current.map(row => row._key === key ? { ...row, [field]: value } : row))
  }

  function addParameter() {
    setRows(current => [...current, {
      instanceName: 'DEFAULT', parameterGroup: activeGroup, parameterKey: '', parameterValue: '',
      valueType: 'STRING', description: '', isSecret: false, activeFl: true,
      _key: `new-${Date.now()}`, _originalGroup: activeGroup, _originalKey: '', _new: true
    }])
    setNotice('New parameter added. Complete the highlighted row and save.')
  }

  async function saveAll() {
    if (rows.some(row => !String(row.parameterKey || '').trim())) {
      setError('Every parameter requires a key before saving.')
      return
    }
    setSaving(true)
    setError('')
    setNotice('')
    try {
      for (const row of rows) {
        const saved = savedRows.find(item => item._originalGroup === row._originalGroup && item._originalKey === row._originalKey)
        if (!row._new && saved && clean(row) === clean(saved)) continue
        const payload = { ...row, originalGroup: row._originalGroup, originalKey: row._originalKey }
        if (row._new) await api.createApplicationParameter(payload)
        else await api.updateApplicationParameter(payload)
      }
      await load('Application parameters saved successfully.')
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setSaving(false)
    }
  }

  async function cancelChanges() {
    if (!window.confirm('Discard all unsaved parameter changes and reload saved values?')) return
    await load('Saved application parameters reloaded.')
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
      await api.deleteApplicationParameter({ instanceName: row.instanceName, parameterGroup: row._originalGroup, parameterKey: row._originalKey })
      setDeleteCandidate(null)
      await load('Application parameter deleted.')
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setSaving(false)
    }
  }

  function toggleSecret(key) {
    setVisibleSecrets(current => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }

  return <div className={`${embedded ? 'settings-parameters-panel' : 'page'} environment-settings-page`}>
    {!embedded && <PageHeader breadcrumb="Application / Configuration" title="Environment & System Settings" subtitle="Maintain table-backed configuration for this KUMO instance." actions={<button className="button primary" type="button" onClick={addParameter} disabled={saving}>+ Add parameter</button>} />}

    {embedded && <div className="settings-parameters-heading"><div><h2>Environment &amp; system</h2><p>Maintain table-backed configuration for this KUMO instance.</p></div><button className="button primary" type="button" onClick={addParameter} disabled={saving}>+ Add parameter</button></div>}

    <div className="parameter-tabs" role="tablist">{groups.map(group => <button key={group.key} role="tab" aria-selected={activeGroup === group.key} onClick={() => setActiveGroup(group.key)}>{group.label}<span>{rows.filter(row => row.parameterGroup === group.key).length}</span></button>)}</div>
    {error && <div className="alert error">{error}</div>}
    {notice && <div className="alert info">{notice}</div>}

    <div className="vision-card parameter-card">
      {loading ? <LoadingState>Loading application parameters…</LoadingState> : visibleRows.length === 0 ? <div className="empty-state">No {activeGroup === 'ENVIRONMENT' ? 'environment variables' : 'system settings'} configured yet.</div> : <div className="parameter-table-scroll"><table className="parameter-table">
        <thead><tr><th>Active</th><th>Key</th><th>Value</th><th>Type</th><th>Description</th><th>Secret</th><th /></tr></thead>
        <tbody>{visibleRows.map(row => <tr key={row._key} className={row._new ? 'parameter-new-row' : ''}>
          <td className="parameter-check"><input type="checkbox" checked={Boolean(row.activeFl)} onChange={event => patch(row._key, 'activeFl', event.target.checked)} /></td>
          <td><input value={row.parameterKey || ''} placeholder="PARAMETER_KEY" onChange={event => patch(row._key, 'parameterKey', event.target.value.toUpperCase())} /></td>
          <td><div className="parameter-value-field"><input type={row.isSecret && !visibleSecrets.has(row._key) ? 'password' : 'text'} value={row.parameterValue || ''} placeholder="Value" onChange={event => patch(row._key, 'parameterValue', event.target.value)} />{row.isSecret && <button type="button" onClick={() => toggleSecret(row._key)}>{visibleSecrets.has(row._key) ? 'Hide' : 'Show'}</button>}</div></td>
          <td><select value={row.valueType || 'STRING'} onChange={event => patch(row._key, 'valueType', event.target.value)}>{valueTypes.map(type => <option key={type}>{type}</option>)}</select></td>
          <td><input value={row.description || ''} placeholder="Description" onChange={event => patch(row._key, 'description', event.target.value)} /></td>
          <td className="parameter-check"><input type="checkbox" checked={Boolean(row.isSecret)} onChange={event => patch(row._key, 'isSecret', event.target.checked)} /></td>
          <td><button className="small-button dependency-delete-button" type="button" onClick={() => setDeleteCandidate(row)} disabled={saving}>Delete</button></td>
        </tr>)}</tbody>
      </table></div>}
      {!loading && <div className="parameter-save-bar"><span>{hasChanges ? 'Unsaved changes' : 'All changes saved'}</span><div><button className="button dependency-cancel-button" type="button" disabled={saving || !hasChanges} onClick={cancelChanges}>Cancel</button><button className="button dependency-save-button" type="button" disabled={saving || !hasChanges} onClick={saveAll}>{saving ? 'Saving…' : 'Save'}</button></div></div>}
    </div>

    {deleteCandidate && <div className="dependency-confirm-backdrop" role="presentation"><div className="dependency-confirm-modal" role="alertdialog" aria-modal="true"><div className="dependency-confirm-icon" aria-hidden="true">×</div><div className="dependency-confirm-copy"><span>Delete application parameter</span><h2>Are you sure?</h2><p><strong>{deleteCandidate.parameterKey || 'This unsaved parameter'}</strong> will be removed. This action cannot be undone.</p></div><div className="dependency-confirm-actions"><button className="button dependency-cancel-button" disabled={saving} onClick={() => setDeleteCandidate(null)}>Cancel</button><button className="button dependency-confirm-delete" disabled={saving} onClick={confirmDelete}>{saving ? 'Deleting…' : 'Delete parameter'}</button></div></div></div>}
  </div>
}
