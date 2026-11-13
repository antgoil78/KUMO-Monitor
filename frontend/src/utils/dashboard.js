// Dashboard and Monitor use the same workflow execution and job hierarchy.
export function dashboardOverview(snapshot = {}) {
  const active = new Set(['SUBMITTING', 'INITIATING', 'UNKNOWN', 'QUEUED', 'CLAIMED', 'RUNNING'])
  const workflows = (snapshot.rows || []).map(workflow => {
    const submission = (snapshot.submissions || []).find(item => item.workflowId === workflow.WORKFLOW_ID && active.has(item.status))
    const pending = submission && (!submission.runId || submission.runId !== workflow.run?.runId)
    const run = pending ? { status: 'INITIATING', requestedAt: submission.submittedAt } : workflow.run || {}
    const lastStatus = run.status === 'CLAIMED' ? 'INITIATING' : run.status || 'NOT RUN'
    return {
      workflowId: workflow.WORKFLOW_ID, workflowName: workflow.WORKFLOW_NAME,
      workflowGroup: workflow.WORKFLOW_GROUP, lastStatus, lastRunId: run.runId,
      lastStartTime: run.start || run.requestedAt,
      jobs: (workflow.jobs || []).filter(job => !job.runOnly),
      progress: pending ? null : { ...workflow.progress, done: workflow.progress?.completed || 0 },
      scheduled: Boolean(workflow.IS_SCHEDULE_ENABLED)
    }
  })
  const count = status => workflows.filter(workflow => workflow.lastStatus === status).length
  const jobs = workflows.flatMap(workflow => workflow.jobs)
  return { workflows, warming: snapshot.warming, error: snapshot.error,
    summary: { total: workflows.length, success: count('SUCCESS'), failed: count('FAILED'),
      running: count('RUNNING'), queued: workflows.filter(workflow => active.has(workflow.lastStatus) && workflow.lastStatus !== 'RUNNING').length,
      jobs: jobs.length, enabledJobs: jobs.filter(job => job.JOB_IS_ENABLED !== false).length,
      failedJobs: jobs.filter(job => job.run?.status === 'FAILED').length,
      scheduled: workflows.filter(workflow => workflow.scheduled).length },
    engine: { status: snapshot.error ? 'FAILED' : snapshot.warming ? 'INITIATING' : snapshot.runsEnabled ? 'READY' : 'DISABLED' }
  }
}
