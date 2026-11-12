// Local prototype only: definitions and per-execution state are separate.
const rule = { trigger: 'DEP', success: null, warning: null, error: null, sql: '', allow: true }
export const sampleConfiguration = [
  { ...rule, id: 'wf1', workflowId: 'wf1', name: 'WF1', jobId: null, parentId: null, trigger: null },
  { ...rule, id: 'wf1-job1', workflowId: 'wf1', name: 'JOB1', jobId: 'job1', parentId: 'wf1' },
  { ...rule, id: 'wf2', workflowId: 'wf2', name: 'WF2', jobId: null, parentId: 'wf1', success: true, warning: true, error: false },
  { ...rule, id: 'wf2-job2', workflowId: 'wf2', name: 'JOB2', jobId: 'job2', parentId: 'wf2', success: true, warning: true, error: false },
  { ...rule, id: 'wf2-job3', workflowId: 'wf2', name: 'JOB3', jobId: 'job3', parentId: 'wf2-job2', success: true, warning: true, error: false },
  { ...rule, id: 'wf3', workflowId: 'wf3', name: 'WF3', jobId: null, parentId: 'wf1', success: true, warning: true, error: false },
  { ...rule, id: 'wf3-job2', workflowId: 'wf3', name: 'JOB2', jobId: 'job2', parentId: 'wf3' },
  { ...rule, id: 'wf3-job4', workflowId: 'wf3', name: 'JOB4', jobId: 'job4', parentId: 'wf3-job2', sql: 'SELECT TRUE' }
]

export function parentOptions(row, rows) {
  return rows.filter(parent => parent.id !== row.id && (row.jobId
    ? parent.workflowId === row.workflowId
    : !parent.jobId))
}

export function validateConfiguration(rows) {
  const errors = []
  const ids = new Set()
  const workflows = rows.filter(row => !row.jobId)
  const byId = new Map(rows.map(row => [row.id, row]))
  const workflowNames = new Set()
  const memberships = new Set()
  for (const row of rows) {
    const label = row.jobId ? `${byId.get(row.workflowId)?.name || '?'} / ${row.name}` : row.name
    const add = message => errors.push({ rowId: row.id, message: `${label}: ${message}` })
    if (row.issue) add(row.issue)
    if (!row.id || ids.has(row.id)) add('Object identity must be unique.')
    ids.add(row.id)
    if (!row.name?.trim()) add('Name is required.')
    if (!['DEP', 'RUNNER', null].includes(row.trigger)) add('Choose DEP, RUNNER or External (NULL).')
    if (typeof row.allow !== 'boolean') add('ALLOW must be true or false.')
    if (['success', 'warning', 'error'].some(key => ![true, false, null].includes(row[key]))) add('Status rules must be Accept, Reject or Ignore.')
    if (!row.jobId) {
      const name = row.name.trim().toLowerCase()
      if (workflowNames.has(name)) add('Workflow name must be unique.')
      workflowNames.add(name)
      if (row.workflowId !== row.id) add('Workflow identity is inconsistent.')
      const roots = rows.filter(job => job.jobId && job.workflowId === row.id && job.parentId === row.id)
      if (rows.some(job => job.jobId && job.workflowId === row.id) && roots.length !== 1) add('Exactly one job must have this workflow as its direct parent.')
    } else {
      const workflow = byId.get(row.workflowId)
      if (!workflow || workflow.jobId) add('A containing workflow is required.')
      const membership = `${row.workflowId}:${row.jobId}`
      if (memberships.has(membership)) add('A job can only occur once in a workflow.')
      memberships.add(membership)
      if (row.trigger !== 'DEP') add('Jobs must use DEP.')
      if (!row.parentId) add('A job requires a parent.')
    }
    if ((row.trigger || ['success', 'warning', 'error'].some(key => row[key] !== null)) && !row.parentId) add('This trigger or status rule requires a parent.')
    if (row.parentId && !parentOptions(row, rows).some(parent => parent.id === row.parentId)) add('Parent must be a workflow, or a job within the same workflow for job rows.')
    const seen = new Set([row.id])
    let parent = byId.get(row.parentId)
    while (parent) {
      if (seen.has(parent.id)) { add('Parent relationships contain a cycle.'); break }
      seen.add(parent.id)
      parent = byId.get(parent.parentId)
    }
  }
  if (!workflows.length) errors.push({ rowId: null, message: 'At least one workflow is required.' })
  return errors
}

export function createRunPreview(rows, scenario = 'example') {
  const cycleId = `preview-${globalThis.crypto.randomUUID()}`
  const runs = Object.fromEntries(rows.map(row => [row.id, {
    runId: `${cycleId}:${row.id}`, workflowRunId: `${cycleId}:${row.workflowId}`,
    parentRunId: row.parentId ? `${cycleId}:${row.parentId}` : null,
    status: 'WAITING', startedAt: null, endedAt: null
  }]))
  if (scenario === 'example') {
    const statuses = { wf1: 'SUCCESS', 'wf1-job1': 'SUCCESS', wf2: 'RUNNING', 'wf2-job2': 'SUCCESS', wf3: 'RUNNING', 'wf3-job2': 'ERROR' }
    for (const [id, status] of Object.entries(statuses)) if (runs[id]) runs[id].status = status
  }
  return runs
}

const terminal = new Set(['SUCCESS', 'WARNING', 'ERROR', 'SKIPPED'])
export function evaluateDependency(row, rows, runs, sqlResults = {}) {
  const result = (state, reason) => ({ state, reason })
  if (validateConfiguration(rows).some(error => error.rowId === row.id)) return result('INVALID', 'Resolve the configuration errors first.')
  if (runs[row.id]?.status === 'RUNNING') return result('RUNNING', 'This execution is already running.')
  if (terminal.has(runs[row.id]?.status)) return result('FINISHED', 'This execution has finished; it will not be dispatched again.')
  if (!row.allow) return result('SKIPPED', 'ALLOW is false.')
  const parent = rows.find(item => item.id === row.parentId)
  const parentRun = parent && runs[parent.id]
  if (parent) {
    if (!parentRun || parentRun.runId !== runs[row.id]?.parentRunId) return result('WAITING', 'No matching parent execution in this preview cycle.')
    if (row.jobId && row.parentId === row.workflowId) {
      if (parentRun.status !== 'RUNNING') return result('WAITING', 'The entry job starts when its containing workflow starts.')
    } else {
      if (!terminal.has(parentRun.status)) return result('WAITING', `Waiting for ${parent.name} to finish.`)
      if (parentRun.status === 'SKIPPED') return result('BLOCKED', `${parent.name} was skipped.`)
      if (row[parentRun.status.toLowerCase()] === false) return result('BLOCKED', `Parent ${parentRun.status.toLowerCase()} is rejected.`)
    }
  }
  if (row.sql.trim()) {
    if (sqlResults[row.id] === 'error') return result('BLOCKED', 'Simulated SQL evaluation failed.')
    if (sqlResults[row.id] === false) return result('SKIPPED', 'SQL condition returned false.')
    if (sqlResults[row.id] !== true) return result('SQL CHECK', 'Choose a simulated SQL result to evaluate this condition.')
  }
  if (row.trigger === 'RUNNER') return result('READY', 'Eligible at the next runner check (every 30 seconds).')
  if (row.trigger === null) return result('READY', 'Eligible when an external trigger arrives.')
  if (row.jobId && row.parentId === row.workflowId) return result('READY', 'Entry job released by its running workflow; completion status flags do not delay entry.')
  return result('READY', 'Parent completed and dependency conditions passed.')
}
