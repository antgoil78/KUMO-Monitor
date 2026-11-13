import test from 'node:test'
import assert from 'node:assert/strict'
import { dashboardOverview } from './dashboard.js'

test('dashboard counts workflow roots and assigned jobs from the new monitor', () => {
  const result = dashboardOverview({ runsEnabled: true, rows: [
    { WORKFLOW_ID: 'a', WORKFLOW_NAME: 'Load', IS_SCHEDULE_ENABLED: true,
      run: { status: 'RUNNING', runId: 'current', start: '2026-11-13T08:00:00Z' },
      progress: { total: 3, completed: 2, percent: 67 },
      jobs: [{ JOB_ID: 'one', run: { status: 'SUCCESS' } }, { JOB_ID: 'two', JOB_IS_ENABLED: false }, { JOB_ID: 'removed', runOnly: true }] },
    { WORKFLOW_ID: 'b', run: { status: 'FAILED' }, jobs: [{ JOB_ID: 'three', run: { status: 'FAILED' } }] },
    { WORKFLOW_ID: 'c', run: { status: 'NOT RUN' }, jobs: [] }
  ] })
  assert.deepEqual(result.summary, { total: 3, success: 0, failed: 1, running: 1, queued: 0, jobs: 3, enabledJobs: 2, failedJobs: 1, scheduled: 1 })
  assert.equal(result.workflows[0].progress.done, 2)
  assert.equal(result.workflows[0].lastRunId, 'current')
  assert.equal(result.engine.status, 'READY')
})

test('pending manual submissions replace previous results while claimed workflows count as queued', () => {
  const result = dashboardOverview({ rows: [
    { WORKFLOW_ID: 'a', run: { status: 'SUCCESS', runId: 'old' }, jobs: [] },
    { WORKFLOW_ID: 'b', run: { status: 'CLAIMED' }, jobs: [] }
  ], submissions: [{ workflowId: 'a', status: 'INITIATING', submittedAt: '2026-11-13T12:00:00Z' }] })
  assert.equal(result.summary.queued, 2)
  assert.equal(result.summary.success, 0)
  assert.equal(result.workflows[0].progress, null)
  assert.equal(result.workflows[0].lastStartTime, '2026-11-13T12:00:00Z')
})
