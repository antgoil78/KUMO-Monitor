import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import './CommandPreview.css'

export default function CommandPreview({ definition }) {
  const payload = JSON.stringify({
    workflowType: definition.workflowType || definition.jobType,
    sqlCommand: definition.sqlCommand,
    dbtCommand: definition.dbtCommand,
    dbtProjectFqn: definition.dbtProjectFqn,
    dbtTarget: definition.dbtTarget
  })
  const generation = useRef(0)
  const [preview, setPreview] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [copied, setCopied] = useState(false)
  const command = JSON.parse(payload)
  const hasCommand = Boolean(String(command.workflowType === 'DBT' ? command.dbtCommand || '' : command.sqlCommand || '').trim())

  useEffect(() => {
    const request = ++generation.current
    setPreview(null)
    setError('')
    setCopied(false)
    setLoading(hasCommand)
    if (!hasCommand) return () => { generation.current++ }
    const timer = window.setTimeout(() => load(request), 500)
    return () => {
      window.clearTimeout(timer)
      generation.current++
    }
  }, [payload, hasCommand])

  async function load(request = ++generation.current) {
    setLoading(true)
    setError('')
    setCopied(false)
    setPreview(null)
    try {
      const result = await api.previewWorkflowCommand(JSON.parse(payload))
      if (request === generation.current) setPreview(result)
    } catch (err) {
      if (request === generation.current) setError(err.message)
    } finally {
      if (request === generation.current) setLoading(false)
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(preview.execSql)
      setCopied(true)
    } catch {
      setError('Could not copy the command. Select the command text to copy it.')
    }
  }

  if (!hasCommand) return null

  return <div className="command-preview">
    <div className="command-preview-heading">
      <strong>Command preview</strong>
      <div className="command-preview-actions">
        <button type="button" className="small-button" onClick={() => load()} disabled={loading}>Refresh</button>
        <button type="button" className="small-button" onClick={copy} disabled={loading || !preview}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
    </div>
    {loading && <div className="command-preview-loading" role="status">Resolving command…</div>}
    {error && <div className="alert error" role="alert">{error}</div>}
    {preview && <div aria-live="polite">
      <pre tabIndex="0"><code>{preview.execSql}</code></pre>
      {preview.runIdIsExample && preview.execSql.includes(preview.runId) && <small>00000000-0000-0000-0000-000000000000 represents the run ID assigned when the workflow starts.</small>}
    </div>}
  </div>
}
