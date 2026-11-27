import test from 'node:test'
import assert from 'node:assert/strict'
import { applySubmission } from './useOrchestrationMonitor.js'

test('durable status wins over a receipt that has not learned its run ID', () => {
  const receipt = { workflowId: 'wf', requestId: 'request', status: 'INITIATING' }
  for (const status of ['QUEUED', 'RUNNING', 'SUCCESS', 'FAILED']) {
    const workflow = { WORKFLOW_ID: 'wf', run: { runId: 'run', requestId: 'request', status }, jobs: [] }
    assert.equal(applySubmission(workflow, [receipt]), workflow)
  }
})

test('a new receipt shows its confirmed queue state and submission time', () => {
  const workflow = { WORKFLOW_ID: 'wf', run: { runId: 'old', status: 'SUCCESS' }, jobs: [] }
  const receipt = { workflowId: 'wf', requestId: 'new', runId: 'new-run', mode: 'WORKFLOW',
    status: 'QUEUED', submittedAt: '2026-11-17T13:00:00Z' }
  const result = applySubmission(workflow, [receipt])
  assert.equal(result.run.status, 'QUEUED')
  assert.equal(result.run.requestedAt, receipt.submittedAt)
  assert.equal(result.run.runId, 'new-run')
  assert.equal(result.run.start, undefined)
})

test('parallel submission prepares every selected job together', () => {
  const workflow = { WORKFLOW_ID: 'wf', JOB_EXECUTION_MODE: 'PARALLEL', run: { status: 'SUCCESS' },
    jobs: ['first', 'second', 'third'].map(JOB_ID => ({ JOB_ID, JOB_IS_ENABLED: true })) }
  const result = applySubmission(workflow, [{ workflowId: 'wf', requestId: 'new', mode: 'FROM_JOB',
    jobId: 'second', status: 'INITIATING' }])
  assert.equal(result.run.jobExecutionMode, 'PARALLEL')
  assert.deepEqual(result.jobs.map(job => job.run.status), ['SKIPPED', 'INITIATING', 'INITIATING'])
})

test('PostgreSQL submission updates State without replacing execution results', () => {
  const workflow = { source: 'POSTGRES', WORKFLOW_ID: 'wf', run: { status: 'SUCCESS', runId: 'old' },
    execution: { status: 'SUCCESS', runId: 'old', start: '2026-11-17T12:00:00Z' },
    planning: { status: 'SCHEDULED', scheduled: true, waitingFor: [] },
    jobs: [{ run: { status: 'SUCCESS' } }], progress: { percent: 100 } }
  const result = applySubmission(workflow, [{ workflowId: 'wf', requestId: 'new', status: 'INITIATING' }])
  assert.equal(result.planning.status, 'INITIATING')
  assert.equal(result.planning.scheduled, true)
  for (const key of ['run', 'execution', 'jobs', 'progress']) assert.equal(result[key], workflow[key])
  assert.equal(result.canRun, false)
})

test('a late PostgreSQL receipt preserves active state and the previous completed result', () => {
  for (const status of ['RUNNING', 'NEEDS REVIEW', 'CANCELLING']) {
    const workflow = { source: 'POSTGRES', WORKFLOW_ID: 'wf', run: { status: 'RUNNING', runId: 'active' },
      lastResult: { status: 'SUCCESS', runId: 'finished' }, planning: { status, message: 'Current activity', waitingCount: 2 } }
    const result = applySubmission(workflow, [{ workflowId: 'wf', requestId: 'new', status: 'INITIATING' }])
    assert.equal(result.lastResult, workflow.lastResult)
    assert.equal(result.run, workflow.run)
    assert.equal(result.planning, workflow.planning)
  }
})

test('PostgreSQL receipts add a separate plan entry and never duplicate a durable request', () => {
  const workflow = { source: 'POSTGRES', WORKFLOW_ID: 'wf', run: { status: 'SUCCESS', runId: 'old' }, jobs: [],
    requests: [{ requestId: 'existing', runId: 'active', status: 'RUNNING', planStatus: 'RUNNING' }] }
  const receipt = { workflowId: 'wf', requestId: 'new', status: 'QUEUED', submittedAt: '2026-11-17T13:00:00Z' }
  const optimistic = applySubmission(workflow, [receipt])
  assert.equal(optimistic.requests.length, 2)
  assert.equal(optimistic.requests[0], workflow.requests[0])
  assert.equal(optimistic.requests[1].requestedAt, receipt.submittedAt)
  assert.equal(applySubmission(optimistic, [receipt]), optimistic)
})
