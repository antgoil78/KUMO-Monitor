const groups = {
  success: ['DONE', 'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK'],
  running: ['RUNNING', 'IN_PROGRESS', 'EXECUTING', 'STARTING'],
  queued: ['INITIATING', 'QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED'],
  failed: ['FAILED', 'FAILURE', 'ERROR'],
  warning: ['WARNING', 'WARN', 'VARNING'],
  info: ['INFO', 'DEBUG', 'TRACE', 'NOTICE'],
  skipped: ['SKIPPED']
}

const iconByKind = {
  success: '✓',
  running: '▶',
  queued: '●',
  failed: '×',
  warning: '!',
  info: 'i',
  skipped: '↷',
  muted: '—'
}

export function statusKind(status) {
  const tokens = String(status || '-').toUpperCase().split(/[^A-ZÀ-ÖØ-Þ0-9]+/).filter(Boolean)
  if (tokens.some(token => groups.failed.includes(token))) return 'failed'
  if (tokens.some(token => groups.warning.includes(token))) return 'warning'
  if (tokens.some(token => groups.success.includes(token))) return 'success'
  if (tokens.some(token => groups.running.includes(token))) return 'running'
  if (tokens.some(token => groups.queued.includes(token))) return 'queued'
  if (tokens.some(token => groups.info.includes(token))) return 'info'
  if (tokens.some(token => groups.skipped.includes(token))) return 'skipped'
  return 'muted'
}

export function isWorkflowBusy(status) {
  return ['running', 'queued'].includes(statusKind(status))
}

export default function StatusBadge({ status, showIcon = true }) {
  const s = String(status || '—').toUpperCase().replace('-', '—')
  const kind = statusKind(s)
  return (
    <span className={`status-badge ${kind}`}>
      {showIcon && <span className="status-icon">{iconByKind[kind]}</span>}
      <span>{s}</span>
    </span>
  )
}
