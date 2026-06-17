export function formatDateTime(value) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Stockholm',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date)
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '-'
  let s = Math.max(0, Math.floor(Number(seconds)))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  s = s % 60
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export function elapsedDuration(startTime, endTime, status, nowMs) {
  const running = ['RUNNING', 'IN_PROGRESS', 'EXECUTING'].includes(String(status || '').toUpperCase())
  if (running && startTime) {
    const startMs = new Date(startTime).getTime()
    if (!Number.isNaN(startMs)) return `${formatDuration((nowMs - startMs) / 1000)} live`
  }
  if (startTime && endTime) {
    const startMs = new Date(startTime).getTime()
    const endMs = new Date(endTime).getTime()
    if (!Number.isNaN(startMs) && !Number.isNaN(endMs)) return formatDuration((endMs - startMs) / 1000)
  }
  return '-'
}
