function EditModal({ workflowId, onClose, onSaved, notify }) {
  const [detail, setDetail] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  function patch(field, value) {
    setDetail(prev => ({ ...prev, [field]: value }))
  }
  function patchNotif(field, value) {
    setDetail(prev => ({ ...prev, notifications: { ...(prev.notifications || {}), [field]: value } }))
  }

  function refreshAfterAction() {
    Promise.resolve(onSaved()).catch(err => {
      notify(`Refresh failed after update: ${err.message}`)
    })
  }

  async function completeAction(request, successMessage, failureMessage) {
    try {
      await request
      notify(successMessage)
      refreshAfterAction()
    } catch (err) {
      notify(`${failureMessage}: ${err.message}`)
    }
  }

  function save() {
    if (saving) return
    setSaving(true)
    setError(null)

    const request = api.updateWorkflow(workflowId, detail)
    notify('Saving workflow...')
    onClose()

    void completeAction(request, 'Workflow saved', 'Failed to save workflow')
  }

  function clone() {
    if (saving) return
    setSaving(true)
    setError(null)

    const request = api.cloneWorkflow(workflowId)
    notify('Cloning workflow...')
    onClose()

    void completeAction(request, 'Workflow cloned and disabled', 'Failed to clone workflow')
  }

  function remove() {
    if (saving) return
    setSaving(true)
    setError(null)

    const request = api.deleteWorkflow(workflowId)
    notify('Deleting workflow...')
    onClose()

    void completeAction(request, 'Workflow deleted', 'Failed to delete workflow')
  }
  return (
    <Modal title="Edit workflow" subtitle={detail?.workflowName || workflowId} onClose={onClose} wide>
      <div />
    </Modal>
  )
}
