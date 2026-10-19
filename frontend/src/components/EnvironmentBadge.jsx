import './EnvironmentBadge.css'

const environments = {
  DEV: { label: 'DEV', tone: 'dev' },
  TEST: { label: 'TEST', tone: 'test' },
  TST: { label: 'TEST', tone: 'test' },
  UAT: { label: 'UAT', tone: 'uat' },
  PROD: { label: 'PROD', tone: 'prod' },
  PRD: { label: 'PROD', tone: 'prod' }
}

export default function EnvironmentBadge({ environment }) {
  const current = environments[String(environment || '').trim().toUpperCase()]
  const label = current?.label || 'UNKNOWN'
  const description = current ? `Service environment: ${label}` : 'Service environment unavailable'

  return (
    <div className="environment-indicator" role="status" aria-label={description} title={description}>
      <strong className={`environment-badge environment-badge--${current?.tone || 'unknown'}`} aria-hidden="true">{label}</strong>
    </div>
  )
}
