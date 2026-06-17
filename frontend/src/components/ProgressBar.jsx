export default function ProgressBar({ progress, status }) {
  const upperStatus = String(status || '').toUpperCase()
  const isRunning = ['RUNNING', 'IN_PROGRESS', 'EXECUTING'].includes(upperStatus)

  if (!progress && !isRunning) return <span className="progress-placeholder">-</span>

  const percent = progress?.percent
  const hasPercent = typeof percent === 'number'
  const width = hasPercent ? Math.max(0, Math.min(100, percent)) : 100
  const label = hasPercent
    ? `${width}%${progress?.total ? ` (${progress.done}/${progress.total})` : ''}`
    : isRunning ? 'Running...' : '-'

  return (
    <div className="progress-wrap" title={label}>
      <div className={`progress-track ${hasPercent ? '' : 'indeterminate'}`}>
        <div className="progress-fill" style={{ width: `${width}%` }} />
      </div>
      <span className="progress-label">{label}</span>
    </div>
  )
}
