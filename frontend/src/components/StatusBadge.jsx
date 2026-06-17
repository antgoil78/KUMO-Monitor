const groups = {
  success: ['SUCCESS', 'SUCCEEDED', 'COMPLETED', 'OK'],
  running: ['RUNNING', 'IN_PROGRESS', 'EXECUTING'],
  queued: ['QUEUED', 'PENDING', 'REQUESTED', 'SCHEDULED'],
  failed: ['FAILED', 'FAILURE', 'ERROR']
}

export function statusKind(status) {
  const s = String(status || '-').toUpperCase()
  if (groups.success.includes(s)) return 'success'
  if (groups.running.includes(s)) return 'running'
  if (groups.queued.includes(s)) return 'queued'
  if (groups.failed.includes(s)) return 'failed'
  return 'muted'
}

export default function StatusBadge({ status }) {
  const s = String(status || '-').toUpperCase()
  return <span className={`status-badge ${statusKind(s)}`}>{s}</span>
}
