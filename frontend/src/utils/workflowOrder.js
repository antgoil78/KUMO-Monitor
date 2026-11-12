export function workflowJobIds(assignments, workflowId) {
  return assignments.filter(row => row.WORKFLOW_ID === workflowId)
    .sort((left, right) => left.JOB_SEQUENCE - right.JOB_SEQUENCE)
    .map(row => row.JOB_ID)
}

export function moveJob(ids, jobId, position) {
  if (!ids.includes(jobId) || !Number.isInteger(position) || position < 0 || position >= ids.length) return ids
  const next = ids.filter(id => id !== jobId)
  next.splice(position, 0, jobId)
  return next
}
