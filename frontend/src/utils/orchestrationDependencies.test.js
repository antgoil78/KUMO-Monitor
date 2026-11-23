import test from 'node:test'
import assert from 'node:assert/strict'
import { addWorkflowParent, createRunPreview, evaluateDependency, removeWorkflowParent, sampleConfiguration, validateConfiguration } from './orchestrationDependencies.js'

const fresh = () => structuredClone(sampleConfiguration)

test('adding and removing parents keeps workflow identities, conditions and job assignments valid', () => {
  const original = fresh()
  const values = { parentId: 'wf3', trigger: 'DEP', success: true, warning: null, error: false,
    sql: 'SELECT TRUE', allow: false, parentMustBeNewer: true }
  let rows = addWorkflowParent(original, 'wf2', values)
  let links = rows.filter(row => !row.jobId && row.workflowId === 'wf2')
  assert.equal(links.length, 2)
  assert.equal(links[1].id.length, 36)
  assert.deepEqual(validateConfiguration(rows), [])
  assert.equal(links[1].sql, 'SELECT TRUE')
  assert.equal(links[1].allow, false)
  assert.deepEqual(rows.filter(row => row.jobId), original.filter(row => row.jobId))

  rows = removeWorkflowParent(rows, links[1].id)
  assert.deepEqual(rows, original)
  rows = addWorkflowParent(rows, 'wf2', values)
  rows = removeWorkflowParent(rows, 'wf2')
  assert.deepEqual(validateConfiguration(rows), [])
  assert.equal(rows.find(row => row.id === 'wf2').parentId, 'wf3')
  assert.equal(rows.find(row => row.id === 'wf2').sql, 'SELECT TRUE')
  assert.deepEqual(rows.filter(row => row.jobId), original.filter(row => row.jobId))

  rows = removeWorkflowParent(rows, 'wf2')
  const external = rows.find(row => row.id === 'wf2')
  for (const field of ['parentId', 'trigger', 'success', 'warning', 'error']) assert.equal(external[field], null)
  assert.equal(external.parentMustBeNewer, false)
  assert.equal(external.sql, '')
  assert.equal(external.allow, true)
  assert.deepEqual(validateConfiguration(rows), [])
  rows = addWorkflowParent(rows, 'wf2', { ...values, trigger: 'RUNNER' })
  assert.equal(rows.filter(row => !row.jobId && row.workflowId === 'wf2').length, 1)
  assert.equal(rows.find(row => row.id === 'wf2').trigger, 'RUNNER')
  assert.deepEqual(validateConfiguration(rows), [])
})

test('additional parents use DEP for every rule while duplicates and cycles are rejected', () => {
  const original = fresh()
  original.find(row => row.id === 'wf2').trigger = 'RUNNER'
  const rows = addWorkflowParent(original, 'wf2', { parentId: 'wf3', trigger: 'RUNNER' })
  assert.ok(rows.filter(row => !row.jobId && row.workflowId === 'wf2').every(row => row.trigger === 'DEP'))
  assert.deepEqual(validateConfiguration(rows), [])
  assert.ok(validateConfiguration(addWorkflowParent(rows, 'wf2', { parentId: 'wf3' })).some(error => error.message.includes('unique dependency')))
  assert.ok(validateConfiguration(addWorkflowParent(rows, 'wf1', { parentId: 'wf2', trigger: 'DEP' })).some(error => error.message.includes('cycle')))
})
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

test('freshness compares parent completion strictly against child last start and allows a first run', () => {
  const rows = fresh(), runs = createRunPreview(rows, 'fresh'), target = rows[2]
  target.parentMustBeNewer = true
  runs.wf1.status = 'SUCCESS'
  runs.wf1.endedAt = '2026-11-17T09:00:00'
  assert.equal(evaluateDependency(target, rows, runs).state, 'READY')
  runs.wf2.lastStartedAt = '2026-11-17T09:00:00'
  assert.equal(evaluateDependency(target, rows, runs).state, 'STALE PARENT')
  runs.wf1.endedAt = '2026-11-17T09:00:01'
  assert.equal(evaluateDependency(target, rows, runs).state, 'READY')
  runs.wf1.endedAt = '2026-11-17T08:59:59'
  target.parentMustBeNewer = false
  assert.equal(evaluateDependency(target, rows, runs).state, 'READY')
})

test('every parent rule passes independently and secondary edges participate in cycle checks', () => {
  const rows = fresh()
  rows.push({ ...rows[2], id: 'wf2-other-rule', parentId: 'wf3', parentMustBeNewer: true })
  assert.deepEqual(validateConfiguration(rows), [])
  const runs = createRunPreview(rows, 'fresh')
  runs.wf1.status = 'SUCCESS'
  assert.equal(evaluateDependency(rows[2], rows, runs).state, 'WAITING')
  runs.wf3.status = 'SUCCESS'
  assert.equal(evaluateDependency(rows[2], rows, runs).state, 'READY')
  runs.wf2.lastStartedAt = '2026-11-17T09:00:00'
  runs.wf3.endedAt = '2026-11-17T08:00:00'
  assert.equal(evaluateDependency(rows[2], rows, runs).state, 'STALE PARENT')
  rows[5].parentId = 'wf2'
  assert.ok(validateConfiguration(rows).some(error => error.message.includes('cycle')))
})
