import { useEffect, useState } from 'react'
import { useBackendHealth } from '../useBackendHealth.js'

export function useConfigurationSource() {
  const [source, setSource] = useState(null)
  const [enabled, setEnabled] = useState(false)
  const { health, error, retry } = useBackendHealth()
  useEffect(() => {
    if (!health) return
    const available = health.environment === 'DEV' && health.taskPrototypeEnabled === true
    setEnabled(available)
    setSource(available && (health.postgresEnginePrimary || window.sessionStorage.getItem('kumoConfigurationSource') !== 'SNOWFLAKE') ? 'POSTGRES' : 'SNOWFLAKE')
  }, [health])
  function change(value) {
    window.sessionStorage.setItem('kumoConfigurationSource', value)
    window.dispatchEvent(new CustomEvent('kumo:configuration-source', { detail: value }))
    setSource(value)
  }
  return { source, enabled, change, error, retry, primary: !!health?.postgresEnginePrimary, trial: source === 'POSTGRES' }
}

export default function ConfigurationSource({ selection, disabled, revision, revisions }) {
  if (selection.error) return <div className="alert error" role="alert">
    Could not determine the configuration source. Retrying automatically…
    <small>{selection.error}</small>
    <button type="button" onClick={selection.retry}>Retry now</button>
  </div>
  if (!selection.enabled) return null
  return <div className="orchestration-context">
    <label>Configuration source <select aria-label="Configuration source" value={selection.source || 'POSTGRES'} disabled={disabled || selection.primary} onChange={event => selection.change(event.target.value)}>
      <option value="POSTGRES">PostgreSQL · DEV workflows</option>
      <option value="SNOWFLAKE">Snowflake · existing workflows</option>
    </select></label>
    {selection.trial && <small>Master configuration{revision ? ` · revision ${revision}` : ''}. Changes apply to new executions; active executions keep their saved settings.</small>}
    {selection.trial && revisions?.length > 0 && <details><summary>Configuration history</summary><ul>{revisions.map(item => <li key={item.revision}>Revision {item.revision} · {item.changed_by} · {new Date(item.changed_at).toLocaleString()}</li>)}</ul></details>}
  </div>
}
