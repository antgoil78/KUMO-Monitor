import test from 'node:test'
import assert from 'node:assert/strict'
import { dependencyScope, dagExecution, dagTone } from './workflowDag.js'

const edges = [['A', 'B'], ['A', 'C'], ['B', 'D'], ['C', 'D'], ['D', 'E']]
  .map(([parent, child]) => ({ PARENT_WORKFLOW_ID: parent, CHILD_WORKFLOW_ID: child }))
const scope = seeds => [...dependencyScope(edges, seeds)].sort()

test('middle workflow retains parallel branches and all join prerequisites', () => {
  assert.deepEqual(scope(['B']), ['A', 'B', 'C', 'D', 'E'])
  assert.deepEqual(scope(['D']), ['A', 'B', 'C', 'D', 'E'])
})
test('parallel branches stay visible even before they join, without adding disconnected groups', () => {
  const separateBranches = [['A', 'B'], ['A', 'C'], ['C', 'F'], ['X', 'Y']]
    .map(([parent, child]) => ({ PARENT_WORKFLOW_ID: parent, CHILD_WORKFLOW_ID: child }))
  assert.deepEqual([...dependencyScope(separateBranches, ['B'])].sort(), ['A', 'B', 'C', 'F'])
})
test('multiple filter matches retain both directions and isolated workflows', () => {
  assert.deepEqual(scope(['B', 'C', 'Independent']), ['A', 'B', 'C', 'D', 'E', 'Independent'])
  assert.deepEqual(scope([]), [])
  assert.deepEqual(scope(['Independent']), ['Independent'])
})
test('traversal tolerates duplicate edges and cycles without looping', () => {
  assert.deepEqual([...dependencyScope([...edges, edges[0], { PARENT_WORKFLOW_ID: 'E', CHILD_WORKFLOW_ID: 'A' }], ['D'])].sort(), ['A', 'B', 'C', 'D', 'E'])
})
test('running workflows retain the latest completed result and additional waiting count', () => {
  assert.deepEqual(dagExecution({ source: 'POSTGRES', run: { status: 'RUNNING' }, lastResult: { status: 'FAILED' }, planning: { status: 'RUNNING', waitingCount: 2 } }), { result: 'FAILED', state: 'RUNNING', waitingCount: 2 })
})
test('jobs show current activity separately from completed results', () => {
  const workflow = { source: 'POSTGRES', planning: { status: 'RUNNING' } }
  assert.deepEqual(dagExecution(workflow, { run: { status: 'PENDING' }, lastResult: { status: 'SUCCESS' } }), { result: 'SUCCESS', state: 'WAITING', waitingCount: 0 })
  assert.deepEqual(dagExecution(workflow, { run: { status: 'UNKNOWN' } }), { result: 'NO RESULT', state: 'NEEDS REVIEW', waitingCount: 0 })
  assert.deepEqual(dagExecution(workflow, { run: { status: 'SUCCESS' }, lastResult: { status: 'SUCCESS' } }), { result: 'SUCCESS', state: 'NO PLAN', waitingCount: 0 })
})
test('current activity overrides historical results for card color', () => {
  assert.equal(dagTone({ state: 'RUNNING', result: 'FAILED' }), 'running')
  assert.equal(dagTone({ state: 'WAITING', result: 'SUCCESS' }), 'waiting')
  assert.equal(dagTone({ state: 'QUEUED', result: 'WARNING' }), 'waiting')
  assert.equal(dagTone({ state: 'BLOCKED', result: 'SUCCESS' }), 'warning')
  assert.equal(dagTone({ state: 'NO PLAN', result: 'SUCCESS' }), 'success')
  assert.equal(dagTone({ state: 'NO PLAN', result: 'WARNING' }), 'warning')
  assert.equal(dagTone({ state: 'NO PLAN', result: 'FAILED' }), 'failed')
  assert.equal(dagTone({ state: 'NO PLAN', result: 'NO RESULT' }), 'neutral')
})
