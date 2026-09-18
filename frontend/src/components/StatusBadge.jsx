const groups = {
  success: ['DONE', 'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK'],
  running: ['RUNNING', 'IN_PROGRESS', 'EXECUTING', 'STARTING'],
  queued: ['INITIATING', 'QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED'],
  failed: ['FAILED', 'FAILURE', 'ERROR'],
  warning: ['WARNING', 'WARN'],
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
  const s = String(status || '-').toUpperCase()
  if (groups.success.includes(s)) return 'success'
  if (groups.running.includes(s)) return 'running'
  if (groups.queued.includes(s)) return 'queued'
  if (groups.failed.includes(s)) return 'failed'
  if (groups.warning.includes(s)) return 'warning'
  if (groups.info.includes(s)) return 'info'
  if (groups.skipped.includes(s)) return 'skipped'
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
