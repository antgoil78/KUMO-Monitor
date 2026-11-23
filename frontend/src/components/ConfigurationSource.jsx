import { useEffect, useState } from 'react'
import { api } from '../api.js'

export function useConfigurationSource() {
  const [source, setSource] = useState(null)
  const [enabled, setEnabled] = useState(false)
  useEffect(() => {
    let alive = true
    api.health().then(health => {
      if (!alive) return
      const available = health.environment === 'DEV' && health.taskPrototypeEnabled === true
      setEnabled(available)
      setSource(available && window.sessionStorage.getItem('kumoConfigurationSource') !== 'SNOWFLAKE' ? 'POSTGRES' : 'SNOWFLAKE')
    }).catch(() => { if (alive) setSource('SNOWFLAKE') })
    return () => { alive = false }
  }, [])
  function change(value) {
    window.sessionStorage.setItem('kumoConfigurationSource', value)
    setSource(value)
  }
  return { source, enabled, change, trial: source === 'POSTGRES' }
}

export default function ConfigurationSource({ selection, disabled, revision, revisions }) {
  if (!selection.enabled) return null
  return <div className="orchestration-context">
    <label>Configuration source <select aria-label="Configuration source" value={selection.source || 'POSTGRES'} disabled={disabled} onChange={event => selection.change(event.target.value)}>
      <option value="POSTGRES">PostgreSQL · WF1–WF4 DEV trial</option>
      <option value="SNOWFLAKE">Snowflake · existing workflows</option>
    </select></label>
    {selection.trial && <small>Master configuration{revision ? ` · revision ${revision}` : ''}. Changes apply to new executions; active executions keep their saved settings. Run the full trial from Monitor.</small>}
    {selection.trial && revisions?.length > 0 && <details><summary>Configuration history</summary><ul>{revisions.map(item => <li key={item.revision}>Revision {item.revision} · {item.changed_by} · {new Date(item.changed_at).toLocaleString()}</li>)}</ul></details>}
  </div>
}
