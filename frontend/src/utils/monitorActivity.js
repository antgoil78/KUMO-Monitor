const activePlans = new Set(['QUEUED', 'RUNNING', 'UNKNOWN'])

export function overlapWaits(run) {
  return (run.waitingFor || []).filter(item => item.overlap || item.reason === 'Waiting for related execution to complete')
}

export function directWaits(run) {
  const dependencies = new Map()
  for (const item of run.waitingFor || []) {
    if (!['PARENT', 'CHILD', 'SELF'].includes(item.relationship) || item.overlap || item.reason === 'Waiting for related execution to complete') continue
    const key = `${item.relationship}:${item.nodeKey || item.name}:${item.cascadeId || run.cascadeId || ''}`
    const status = item.status === 'ADMITTED' ? 'QUEUED' : item.status === 'STALE_PARENT' ? 'STALE' : item.status || 'MISSING'
    // A dependency is one workflow occurrence in one plan. Keep a rule
    // rejection (such as stale output) instead of appending a second status.
    if (!dependencies.has(key) || ['STALE', 'MISSING'].includes(status)) dependencies.set(key, { ...item, statuses: new Set([status]) })
  }
  return [...dependencies.values()]
}

export function requestStatus(request) {
  if (request.cancelRequested) return 'CANCELLING'
  if (request.dispatch?.canRetry && ['WAITING', 'PENDING', 'QUEUED'].includes(request.status)) return 'BLOCKED'
  return { WAITING: 'PLANNED', PENDING: 'PLANNED', RUNNING: 'STARTED', SUCCESS: 'SUCCEEDED',
    UNKNOWN: 'NEEDS REVIEW', INITIATING: 'QUEUED', CLAIMED: 'QUEUED' }[request.status] || request.status
}

export function planRequests(workflow, job) {
  return (workflow.requests || []).filter(request => activePlans.has(request.planStatus)).flatMap(request => {
    if (!job) return [request]
    const execution = request.jobs?.find(item => item.jobId === job.JOB_ID)
    return execution ? [{ ...request, ...execution, requestRunId: request.runId,
      waitingFor: request.waitingFor, dispatch: request.dispatch, trigger: request.trigger,
      cancelRequested: request.cancelRequested && !['SUCCESS', 'WARNING', 'FAILED', 'SKIPPED', 'CANCELLED'].includes(execution.status) }] : []
  })
}

export function matchesActivity(workflow, status) {
  const requests = planRequests(workflow)
  return !status || (status === 'NO PLAN' ? !requests.length : requests.some(request => requestStatus(request) === status))
}

export function requestProgress(request) {
  const jobs = request.jobs || []
  const finished = jobs.filter(job => ['SUCCESS', 'WARNING', 'FAILED', 'SKIPPED', 'CANCELLED'].includes(job.status)).length
  return { total: jobs.length, finished, percent: jobs.length ? Math.round(100 * finished / jobs.length) : 0,
    failed: jobs.some(job => job.status === 'FAILED') }
}
