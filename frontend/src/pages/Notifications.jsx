import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import './Notifications.css'


function asBoolean(value) {
  if (value === null || value === undefined) return null
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (['true', '1', 'yes', 'y', 'on'].includes(normalized)) return true
    if (['false', '0', 'no', 'n', 'off'].includes(normalized)) return false
  }
  return Boolean(value)
}


function BoolMark({ value }) {
  const enabled = asBoolean(value)

  if (enabled === null) {
    return (
      <span className="notification-bool neutral" title="Not configured">
        —
      </span>
    )
  }

  return (
    <span
      className={`notification-bool ${enabled ? 'enabled' : 'disabled'}`}
      title={enabled ? 'Enabled' : 'Disabled'}
    >
      {enabled ? '✓' : '✗'}
    </span>
  )
}


function GroupEditor({ editor, saving, onChange, onSave, onClose }) {
  if (!editor) return null

  const editing = editor.mode === 'edit'

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose()
      }}
    >
      <div className="vision-modal notification-group-modal" role="dialog" aria-modal="true">
        <div className="modal-header">
          <div>
            <span className="modal-eyebrow">
              {editing ? 'Email group' : 'New email group'}
            </span>
            <h2>{editing ? `Edit: ${editor.groupName}` : 'Add Email Group'}</h2>
            <p>
              {editing
                ? 'Update recipients and description for this distribution group.'
                : 'Create a reusable email distribution group for workflow notifications.'}
            </p>
          </div>

          <button
            type="button"
            className="modal-close"
            disabled={saving}
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <form className="notification-editor-form" onSubmit={onSave}>
          <div className="form-field">
            <label htmlFor="notification-group-name">Group Name</label>
            <input
              id="notification-group-name"
              value={editor.groupName}
              disabled={editing || saving}
              placeholder="e.g. ops-team"
              autoFocus={!editing}
              onChange={(event) => onChange('groupName', event.target.value)}
            />
          </div>

          <div className="form-field">
            <label htmlFor="notification-group-recipients">Recipients</label>
            <textarea
              id="notification-group-recipients"
              rows={4}
              value={editor.recipients}
              disabled={saving}
              placeholder="email1@company.com, email2@company.com"
              onChange={(event) => onChange('recipients', event.target.value)}
            />
            <span className="notification-field-help">
              Separate multiple email addresses with commas.
            </span>
          </div>

          <div className="form-field">
            <label htmlFor="notification-group-description">Description</label>
            <input
              id="notification-group-description"
              value={editor.description}
              disabled={saving}
              placeholder="Operations team alerts"
              onChange={(event) => onChange('description', event.target.value)}
            />
          </div>

          <div className="notification-modal-actions">
            <button
              type="submit"
              className="button primary"
              disabled={saving || !editor.groupName.trim()}
            >
              {saving ? 'Saving…' : editing ? 'Save Changes' : 'Create Group'}
            </button>

            <button
              type="button"
              className="button muted"
              disabled={saving}
              onClick={onClose}
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}


export default function Notifications() {
  const [activeTab, setActiveTab] = useState('groups')
  const [groups, setGroups] = useState([])
  const [workflows, setWorkflows] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)
  const [editor, setEditor] = useState(null)
  const [groupSearch, setGroupSearch] = useState('')
  const [workflowSearch, setWorkflowSearch] = useState('')

  async function load() {
    setLoading(true)
    try {
      setError(null)
      const data = await api.notifications()
      setGroups(data.groups || [])
      setWorkflows(data.workflows || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const filteredGroups = useMemo(() => {
    const term = groupSearch.trim().toLowerCase()
    if (!term) return groups

    return groups.filter((row) =>
      [row.GROUP_NAME, row.RECIPIENTS, row.DESCRIPTION]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(term))
    )
  }, [groups, groupSearch])

  const filteredWorkflows = useMemo(() => {
    const term = workflowSearch.trim().toLowerCase()
    if (!term) return workflows

    return workflows.filter((row) =>
      [
        row.WORKFLOW_NAME,
        row.WORKFLOW_GROUP,
        row.FAIL_GROUP,
        row.SUCCESS_GROUP,
        row.EMAIL_INTEGRATION,
        row.ENVIRONMENT,
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(term))
    )
  }, [workflows, workflowSearch])

  function showMessage(text) {
    setMessage(text)
    window.setTimeout(() => setMessage(null), 4500)
  }

  function openCreate() {
    setError(null)
    setEditor({
      mode: 'create',
      groupName: '',
      recipients: '',
      description: '',
    })
  }

  function openEdit(row) {
    setError(null)
    setEditor({
      mode: 'edit',
      groupName: String(row.GROUP_NAME || ''),
      recipients: String(row.RECIPIENTS || ''),
      description: String(row.DESCRIPTION || ''),
    })
  }

  function changeEditor(field, value) {
    setEditor((current) => ({ ...current, [field]: value }))
  }

  async function saveGroup(event) {
    event.preventDefault()
    if (!editor) return

    const groupName = editor.groupName.trim()
    if (!groupName) {
      setError('Group name is required')
      return
    }

    setSaving(true)
    setError(null)

    try {
      const payload = {
        recipients: editor.recipients || '',
        description: editor.description || '',
      }

      if (editor.mode === 'edit') {
        await api.updateNotificationGroup(groupName, payload)
        showMessage(`Saved ${groupName}`)
      } else {
        await api.createNotificationGroup({ groupName, ...payload })
        showMessage(`Created ${groupName}`)
      }

      setEditor(null)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function removeGroup(row) {
    const groupName = String(row.GROUP_NAME || '')
    if (!groupName) return

    const confirmed = window.confirm(
      `Delete email group "${groupName}"?\n\nThis removes the distribution group from KUMO. It does not change the Snowflake notification integration allowed-recipient list.`
    )
    if (!confirmed) return

    setError(null)

    try {
      await api.deleteNotificationGroup(groupName)
      showMessage(`Deleted ${groupName}`)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <section className="page notifications-page">
      <div className="page-hero">
        <div>
          <p className="eyebrow">KUMO Monitor</p>
          <h1 className="page-heading">Notifications</h1>
          <p className="page-subtitle">
            Manage email distribution groups and review notification settings across workflows.
          </p>
        </div>

        <button
          type="button"
          className="refresh-button"
          onClick={load}
          disabled={loading}
        >
          {loading ? 'Refreshing…' : '↻ Refresh'}
        </button>
      </div>

      {error && <div className="alert error notification-alert">{error}</div>}
      {message && <div className="notification-toast">{message}</div>}

      <div className="notification-tabs" role="tablist" aria-label="Notification administration">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'groups'}
          className={`notification-tab ${activeTab === 'groups' ? 'active' : ''}`}
          onClick={() => setActiveTab('groups')}
        >
          <span>Email Groups</span>
          <span className="notification-tab-count">{groups.length}</span>
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'config'}
          className={`notification-tab ${activeTab === 'config' ? 'active' : ''}`}
          onClick={() => setActiveTab('config')}
        >
          <span>Workflow Config</span>
          <span className="notification-tab-count">{workflows.length}</span>
        </button>
      </div>

      {activeTab === 'groups' && (
        <div className="notification-panel">
          <div className="notification-integration-note">
            <div className="notification-note-icon">i</div>
            <div>
              <strong>Snowflake notification integration</strong>
              <p>
                When adding new email addresses to a group, each address must also be present in
                the Snowflake notification integration. Run the following as ACCOUNTADMIN and use
                the complete list of allowed recipients:
              </p>
              <code>
                ALTER NOTIFICATION INTEGRATION MY_EMAIL_INT SET ALLOWED_RECIPIENTS =
                ('email1@company.com', 'email2@company.com', ...)
              </code>
            </div>
          </div>

          <div className="notification-panel-head">
            <div>
              <h2>Email Groups</h2>
              <p>Reusable recipient groups used by workflow notification rules.</p>
            </div>

            <div className="notification-panel-actions">
              <input
                className="notification-search"
                value={groupSearch}
                placeholder="Search groups…"
                aria-label="Search email groups"
                onChange={(event) => setGroupSearch(event.target.value)}
              />
              <button type="button" className="button primary" onClick={openCreate}>
                + Add Group
              </button>
            </div>
          </div>

          <div className="table-card notification-table-card">
            {loading ? (
              <div className="notification-empty-state">Loading email groups…</div>
            ) : filteredGroups.length === 0 ? (
              <div className="notification-empty-state">
                {groups.length === 0 ? 'No email groups configured.' : 'No groups match your search.'}
              </div>
            ) : (
              <table className="notification-table notification-groups-table">
                <thead>
                  <tr>
                    <th>Group</th>
                    <th>Recipients</th>
                    <th>Description</th>
                    <th className="notification-actions-heading">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredGroups.map((row) => {
                    const groupName = String(row.GROUP_NAME || '')
                    return (
                      <tr key={groupName}>
                        <td>
                          <strong className="notification-group-name">{groupName}</strong>
                        </td>
                        <td>
                          <span className="notification-recipient-list">
                            {row.RECIPIENTS || '—'}
                          </span>
                        </td>
                        <td>
                          <span className="notification-description">
                            {row.DESCRIPTION || '—'}
                          </span>
                        </td>
                        <td>
                          <div className="notification-row-actions">
                            <button
                              type="button"
                              className="small-button notification-edit-button"
                              onClick={() => openEdit(row)}
                            >
                              Edit
                            </button>
                            <button
                              type="button"
                              className="small-button notification-delete-button"
                              onClick={() => removeGroup(row)}
                            >
                              Delete
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {activeTab === 'config' && (
        <div className="notification-panel">
          <div className="notification-panel-head">
            <div>
              <h2>Workflow Config</h2>
              <p>Email notification settings per workflow. Configuration remains edited from the workflow editor.</p>
            </div>

            <div className="notification-panel-actions">
              <input
                className="notification-search"
                value={workflowSearch}
                placeholder="Search workflows…"
                aria-label="Search workflow notification configuration"
                onChange={(event) => setWorkflowSearch(event.target.value)}
              />
            </div>
          </div>

          <div className="table-card notification-table-card">
            {loading ? (
              <div className="notification-empty-state">Loading workflow settings…</div>
            ) : filteredWorkflows.length === 0 ? (
              <div className="notification-empty-state">
                {workflows.length === 0 ? 'No workflows found.' : 'No workflows match your search.'}
              </div>
            ) : (
              <table className="notification-table notification-config-table">
                <thead>
                  <tr>
                    <th>Workflow</th>
                    <th>On Fail</th>
                    <th>Fail Group</th>
                    <th>On Success</th>
                    <th>Success Group</th>
                    <th>Integration</th>
                    <th>Env</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredWorkflows.map((row) => (
                    <tr key={row.WORKFLOW_ID || row.WORKFLOW_NAME}>
                      <td>
                        <div className="notification-workflow-cell">
                          <strong>{row.WORKFLOW_NAME || 'Unnamed workflow'}</strong>
                          {row.WORKFLOW_GROUP && <span>{row.WORKFLOW_GROUP}</span>}
                        </div>
                      </td>
                      <td><BoolMark value={row.ON_FAIL_EMAIL} /></td>
                      <td>{row.FAIL_GROUP || '—'}</td>
                      <td><BoolMark value={row.ON_SUCCESS_EMAIL} /></td>
                      <td>{row.SUCCESS_GROUP || '—'}</td>
                      <td className="notification-muted">{row.EMAIL_INTEGRATION || '—'}</td>
                      <td className="notification-muted">{row.ENVIRONMENT || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      <GroupEditor
        editor={editor}
        saving={saving}
        onChange={changeEditor}
        onSave={saveGroup}
        onClose={() => !saving && setEditor(null)}
      />
    </section>
  )
}
