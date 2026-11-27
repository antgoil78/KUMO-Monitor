import test from 'node:test'
import assert from 'node:assert/strict'
import { directWaits, overlapWaits, matchesActivity, planRequests, requestProgress, requestStatus } from './monitorActivity.js'

test('completed workflow entries stay only while their plan is active', () => {
  const workflow = { requests: [{ runId: 'one', status: 'SUCCESS', planStatus: 'RUNNING' }, { runId: 'two', status: 'FAILED', planStatus: 'FAILED' }] }
  assert.deepEqual(planRequests(workflow).map(requestStatus), ['SUCCEEDED'])
  workflow.requests[0].planStatus = 'SUCCESS'
  assert.deepEqual(planRequests(workflow), [])
  assert.equal(matchesActivity(workflow, 'NO PLAN'), true)
})
test('request labels distinguish waiting, native queueing and actual execution', () => {
  assert.equal(requestStatus({ status: 'WAITING' }), 'PLANNED')
  assert.equal(requestStatus({ status: 'QUEUED' }), 'QUEUED')
  assert.equal(requestStatus({ status: 'RUNNING' }), 'STARTED')
  assert.equal(requestStatus({ status: 'UNKNOWN' }), 'NEEDS REVIEW')
  assert.equal(requestStatus({ status: 'WAITING', dispatch: { canRetry: true } }), 'BLOCKED')
})
test('each job occurrence uses its own plan, result and parent explanation', () => {
  const workflow = { requests: [
    { runId: 'first', planStatus: 'RUNNING', waitingFor: [{ name: 'A' }], jobs: [{ jobId: 'job', status: 'SUCCESS' }] },
    { runId: 'second', planStatus: 'QUEUED', waitingFor: [{ name: 'B' }], jobs: [{ jobId: 'job', status: 'PENDING' }] }
  ] }
  const requests = planRequests(workflow, { JOB_ID: 'job' })
  assert.deepEqual(requests.map(requestStatus), ['SUCCEEDED', 'PLANNED'])
  assert.deepEqual(requests.map(request => request.waitingFor[0].name), ['A', 'B'])
  assert.deepEqual(requests.map(request => request.requestRunId), ['first', 'second'])
  assert.equal(matchesActivity(workflow, 'NO PLAN'), false)
})
test('progress is scoped to one execution request and includes skipped jobs', () => {
  assert.deepEqual(requestProgress({ jobs: [{ status: 'SUCCESS' }, { status: 'RUNNING' }, { status: 'SKIPPED' }] }),
    { total: 3, finished: 2, percent: 67, failed: false })
})

test('previous plan overlap never adds its status to the current parent occurrence', () => {
  const previous = { name: 'WF8', nodeKey: 'wf8', relationship: 'PARENT', cascadeId: 'previous', status: 'RUNNING', overlap: true }
  const current = { ...previous, cascadeId: 'current', status: 'WAITING', overlap: false }
  const run = { cascadeId: 'current', waitingFor: [previous, current, { ...current }] }
  const parents = directWaits(run)
  assert.equal(parents.length, 1)
  assert.equal(parents[0].cascadeId, 'current')
  assert.deepEqual([...parents[0].statuses], ['WAITING'])
  assert.deepEqual(overlapWaits(run), [previous])
  // Also handles a previously fetched payload before the API restarts.
  run.waitingFor[0] = { ...previous, overlap: undefined, reason: 'Waiting for related execution to complete' }
  assert.deepEqual([...directWaits(run)[0].statuses], ['WAITING'])
  assert.equal(overlapWaits(run).length, 1)
})

test('dependency statuses remain distinct by plan and rule rejection uses one badge', () => {
  const parent = { name: 'WF8', nodeKey: 'wf8', relationship: 'PARENT', cascadeId: 'one', status: 'SUCCESS' }
  const parents = directWaits({ waitingFor: [parent, { ...parent, status: 'STALE_PARENT' }, { ...parent, cascadeId: 'two', status: 'RUNNING' }] })
  assert.deepEqual(parents.map(item => [item.cascadeId, [...item.statuses]]), [['one', ['STALE']], ['two', ['RUNNING']]])
})
