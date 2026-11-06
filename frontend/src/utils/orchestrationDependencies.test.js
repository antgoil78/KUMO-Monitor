import test from 'node:test'
import assert from 'node:assert/strict'
import { createRunPreview, evaluateDependency, sampleConfiguration, validateConfiguration } from './orchestrationDependencies.js'

const fresh = () => structuredClone(sampleConfiguration)
test('example is valid; jobs may be reused across workflows but not repeated within one', () => {
  const rows = fresh()
  assert.deepEqual(validateConfiguration(rows), [])
  rows.push({ ...rows[3], id: 'duplicate' })
  assert.ok(validateConfiguration(rows).some(error => error.message.includes('only occur once')))
})
test('requires one entry job and rejects cycles and cross-workflow job parents', () => {
  const rows = fresh()
  rows[4].parentId = 'wf2'
  assert.ok(validateConfiguration(rows).some(error => error.message.includes('Exactly one')))
  rows[4].parentId = 'wf3-job2'
  assert.ok(validateConfiguration(rows).some(error => error.rowId === rows[4].id))
  rows[4].parentId = 'wf2-job2'
  rows[3].parentId = 'wf2-job3'
  assert.ok(validateConfiguration(rows).some(error => error.message.includes('cycle')))
})
test('workflow parents and trigger/status parent requirements are enforced', () => {
  const rows = fresh()
  rows[0].trigger = 'RUNNER'
  assert.ok(validateConfiguration(rows).some(error => error.message.includes('requires a parent')))
  rows[0].trigger = null
  rows[0].success = false
  assert.ok(validateConfiguration(rows).some(error => error.rowId === 'wf1'))
  rows[0].success = null
  rows[2].parentId = 'wf1-job1'
  assert.ok(validateConfiguration(rows).some(error => error.rowId === 'wf2'))
})
test('parent status flags select the actual outcome; null ignores and false rejects', () => {
  const rows = fresh(), runs = createRunPreview(rows, 'fresh'), target = rows[2]
  runs.wf1.status = 'WARNING'
  assert.equal(evaluateDependency(target, rows, runs).state, 'READY')
  runs.wf1.status = 'ERROR'
  assert.equal(evaluateDependency(target, rows, runs).state, 'BLOCKED')
  target.error = null
  assert.equal(evaluateDependency(target, rows, runs).state, 'READY')
  runs.wf1.status = 'RUNNING'
  assert.equal(evaluateDependency(target, rows, runs).state, 'WAITING')
})
test('entry job starts with workflow; reused job executions remain independent', () => {
  const rows = fresh(), runs = createRunPreview(rows)
  assert.notEqual(runs['wf2-job2'].runId, runs['wf3-job2'].runId)
  assert.notEqual(runs['wf2-job2'].workflowRunId, runs['wf3-job2'].workflowRunId)
  assert.equal(evaluateDependency(rows[4], rows, runs).state, 'READY')
  runs['wf2-job2'].status = 'WAITING'
  assert.equal(evaluateDependency(rows[3], rows, runs).state, 'READY')
  runs.wf2.status = 'WAITING'
  assert.equal(evaluateDependency(rows[3], rows, runs).state, 'WAITING')
})
test('ALLOW and SQL gate execution without mutating definitions', () => {
  const rows = fresh(), before = structuredClone(rows), runs = createRunPreview(rows), target = rows[7]
  assert.equal(evaluateDependency(target, rows, runs).state, 'SQL CHECK')
  assert.equal(evaluateDependency(target, rows, runs, { [target.id]: false }).state, 'SKIPPED')
  assert.equal(evaluateDependency(target, rows, runs, { [target.id]: true }).state, 'READY')
  assert.equal(evaluateDependency(target, rows, runs, { [target.id]: 'error' }).state, 'BLOCKED')
  assert.deepEqual(rows, before)
  target.allow = false
  assert.equal(evaluateDependency(target, rows, runs, { [target.id]: true }).state, 'SKIPPED')
  target.allow = true
  target.error = false
  assert.equal(evaluateDependency(target, rows, runs, { [target.id]: true }).state, 'BLOCKED')
})
test('old parent runs cannot release this execution; finished objects do not redispatch', () => {
  const rows = fresh(), runs = createRunPreview(rows, 'fresh')
  runs.wf1.status = 'SUCCESS'
  runs.wf2.parentRunId = 'old-parent-run'
  assert.equal(evaluateDependency(rows[2], rows, runs).state, 'WAITING')
  runs.wf2.status = 'SUCCESS'
  assert.equal(evaluateDependency(rows[2], rows, runs).state, 'FINISHED')
})
