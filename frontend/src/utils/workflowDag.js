// Keep the whole connected dependency group, including parallel branches.
// Selecting one branch must not hide another branch or a join prerequisite.
export function dependencyScope(dependencies, seeds) {
  const result = new Set(seeds.map(String))
  const adjacent = new Map()
  for (const edge of dependencies) {
    const parent = String(edge.PARENT_WORKFLOW_ID), child = String(edge.CHILD_WORKFLOW_ID)
    adjacent.set(parent, [...(adjacent.get(parent) || []), child])
    adjacent.set(child, [...(adjacent.get(child) || []), parent])
  }
  const pending = [...result]
  while (pending.length) {
    for (const id of adjacent.get(pending.pop()) || []) {
      if (!result.has(id)) { result.add(id); pending.push(id) }
    }
  }
  return result
}

export function dagTone({ state, result }) {
  if (state === 'RUNNING') return 'running'
  if (['NEEDS REVIEW', 'BLOCKED'].includes(state)) return 'warning'
  if (['WAITING', 'QUEUED', 'INITIATING', 'CLAIMED', 'CANCELLING'].includes(state)) return 'waiting'
  if (['FAILED', 'ERROR', 'FAILURE'].includes(result)) return 'failed'
  if (['WARNING', 'WARNINGS', 'WARN'].includes(result)) return 'warning'
  if (['SUCCESS', 'SUCCEEDED', 'COMPLETED', 'DONE'].includes(result)) return 'success'
  return 'neutral'
}

export function dagExecution(workflow, job) {
  if (!workflow) return { result: '—', state: 'UNAVAILABLE' }
  const postgres = workflow.source === 'POSTGRES'
  const current = job?.run?.status || workflow.run?.status
  const planning = workflow.planning?.status || 'NO PLAN'
  const state = job ? current === 'UNKNOWN' ? 'NEEDS REVIEW'
    : ['RUNNING', 'QUEUED'].includes(current) ? current : current === 'PENDING' ? 'WAITING'
      : !job.runOnly && job.JOB_IS_ENABLED !== false && planning !== 'RUNNING' ? planning : 'NO PLAN'
    : postgres ? planning : ['RUNNING', 'QUEUED', 'INITIATING', 'CLAIMED'].includes(current) ? current : 'NO PLAN'
  const result = postgres ? (job || workflow).lastResult?.status || 'NO RESULT'
    : ['RUNNING', 'QUEUED', 'INITIATING', 'CLAIMED', 'PENDING'].includes(current) ? '—' : current || 'NO RESULT'
  return { result, state, waitingCount: job ? 0 : workflow.planning?.waitingCount || 0 }
}
