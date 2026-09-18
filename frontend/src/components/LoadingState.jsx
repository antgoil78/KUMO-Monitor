export function LoadingSpinner({ label = 'Loading' }) {
  return <span className="loading-spinner" role="status" aria-label={label}><i /><i /><i /></span>
}

export default function LoadingState({ children = 'Loading…', className = '' }) {
  return (
    <div className={`loading-state ${className}`.trim()} role="status" aria-live="polite">
      <LoadingSpinner label={String(children)} />
      <span>{children}</span>
    </div>
  )
}
