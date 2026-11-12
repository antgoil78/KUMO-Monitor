import test from 'node:test'
import assert from 'node:assert/strict'
import { barGeometry, connectedExecutions, executionInterval, executionParents, parseStockholmInput, preciseDuration, stockholmInput, timelineLanes, timelineLayout, timelineTicks } from './orchestrationTimeline.js'

test('Stockholm custom ranges respect winter, summer and DST transitions', () => {
  assert.equal(parseStockholmInput('2026-11-11T13:00'), Date.parse('2026-11-11T12:00:00Z'))
  assert.equal(parseStockholmInput('2026-07-11T13:00:01'), Date.parse('2026-07-11T11:00:01Z'))
  assert.equal(parseStockholmInput('2026-03-29T02:30'), null)
  assert.equal(parseStockholmInput('2026-10-25T02:30'), Date.parse('2026-10-25T00:30:00Z'))
  assert.equal(parseStockholmInput('bad'), null)
  assert.equal(stockholmInput(Date.parse('2026-11-11T23:00:00Z')), '2026-11-12T00:00:00')
})

test('execution intervals extend live runs, retain subsecond durations and do not invent terminal durations', () => {
  const run = { start: '2026-11-11T12:00:00.100Z', end: '2026-11-11T12:00:00.225Z', status: 'SUCCESS' }
  assert.equal(preciseDuration(run, Date.now()), '125 ms')
  assert.equal(preciseDuration({ ...run, end: '2026-11-11T12:00:01.225Z' }, Date.now()), '1.125 s')
  const start = Date.parse(run.start), now = start + 5000
  assert.deepEqual(executionInterval({ ...run, end: null, status: 'RUNNING' }, now), { start, end: now })
  assert.deepEqual(executionInterval({ ...run, end: null, status: 'FAILED' }, now), { start, end: start })
  assert.equal(preciseDuration({ ...run, end: null, status: 'FAILED' }, now), 'Duration unavailable')
  assert.equal(executionInterval({ status: 'NOT RUN' }, now), null)
})

test('viewport clips both ends precisely and excludes events outside it', () => {
  assert.deepEqual(barGeometry({ start: -100, end: 1200 }, 0, 1000), { left: 0, width: 100, clippedStart: true, clippedEnd: true })
  assert.equal(barGeometry({ start: -100, end: -1 }, 0, 1000), null)
  assert.equal(barGeometry({ start: 1001, end: 1002 }, 0, 1000), null)
  assert.equal(barGeometry({ start: 500, end: 501 }, 0, 1000).width, .1)
  assert.equal(barGeometry({ start: 1000, end: 1000 }, 0, 1000).width, 0)
})

test('overlapping runs and tiny hit targets get separate lanes; disjoint runs reuse lanes', () => {
  const run = (id, start, end) => ({ executionId: id, status: 'SUCCESS', start: new Date(start).toISOString(), end: new Date(end).toISOString() })
  const lanes = timelineLanes([run('first', 100, 400), run('overlap', 200, 300), run('next', 500, 600), run('tiny', 501, 502)], 0, 1000, 1000, 1000)
  assert.equal(lanes.count, 2)
  assert.deepEqual(lanes.items.map(item => [item.run.executionId, item.lane]), [['first', 0], ['overlap', 1], ['next', 0], ['tiny', 1]])
  assert.equal(timelineLanes([], 0, 1000, 1000, 1000).count, 1)
})

test('ticks adapt to pixels and support subsecond detail without an unbounded grid', () => {
  const fine = timelineTicks(0, 1000, 1000), coarse = timelineTicks(0, 86400000, 1000)
  assert.ok(fine.step < 1000)
  assert.ok(coarse.step >= 3600000)
  assert.ok(fine.ticks.length <= 10)
  assert.ok(coarse.ticks.length <= 10)
  assert.ok(timelineTicks(0, 86400000, 8000).ticks.length > coarse.ticks.length)
})

const pointRun = (executionId, start, end, extra = {}) => ({ workflowId: 'workflow', runId: 'root', executionId, status: 'SUCCESS', start: new Date(start).toISOString(), end: new Date(end).toISOString(), ...extra })

test('relationships use exact parent IDs and exclude omitted jobs across repeated executions', () => {
  const first = pointRun('root', 100, 600, { jobs: [
    pointRun('a', 100, 200, { position: 1, parentExecutionId: 'root', selected: true }),
    pointRun('omitted', 100, 100, { position: 2, parentExecutionId: 'root', selected: false }),
    pointRun('b', 300, 600, { position: 3, parentExecutionId: 'a', selected: true })
  ] })
  const repeated = pointRun('repeat', 700, 900, { runId: 'repeat', jobs: [pointRun('c', 700, 800, { runId: 'repeat', position: 1, parentExecutionId: 'repeat', selected: true })] })
  const parents = executionParents([first, repeated])
  assert.equal(parents.get('workflow:b'), 'a')
  assert.equal(parents.get('workflow:c'), 'repeat')
  assert.equal(parents.has('workflow:omitted'), false)
  assert.deepEqual([...connectedExecutions([first, repeated], first.jobs[2])].sort(), ['workflow:a', 'workflow:b', 'workflow:root'])
})

test('cached execution order stays within the same run and an explicit null parent is respected', () => {
  const root = pointRun('root', 0, 1000, { jobs: [pointRun('b', 200, 300, { position: 2 }), pointRun('a', 100, 200, { position: 1 }), pointRun('orphan', 400, 500, { position: 3, parentExecutionId: null })] })
  const parents = executionParents([root])
  assert.equal(parents.get('workflow:a'), 'root')
  assert.equal(parents.get('workflow:b'), 'a')
  assert.equal(parents.has('workflow:orphan'), false)
})

test('connector coordinates align to stacked lanes and tiny bars at viewport boundaries', () => {
  const root = pointRun('root', -10, 1000, { jobs: [pointRun('child', 1000, 1100, { position: 1, selected: true, parentExecutionId: 'root' })] })
  const rows = [{ key: 'workflow', runs: [root], workflow: {} }, { key: 'job', job: {}, runs: root.jobs }]
  const layout = timelineLayout(rows, [root], 0, 1000, 1000, 800)
  assert.equal(layout.connections.length, 1)
  const link = layout.connections[0]
  assert.equal(link.fromX, 0)
  assert.equal(link.toX, 795)
  assert.equal(link.fromY, 29)
  assert.equal(link.toY, 60)
  assert.equal(layout.height, 108)
  const compact = timelineLayout(rows, [root], 0, 1000, 1000, 800, true)
  assert.equal(compact.height, 80)
  assert.equal(compact.connections[0].fromY, 24)
  assert.equal(compact.connections[0].toY, 46)
  assert.equal(timelineLayout(rows.slice(0, 1), [root], 0, 1000, 1000, 800).connections.length, 0)
})

test('cross-workflow lines require a stored execution parent rather than nearby timestamps', () => {
  const parent = pointRun('root', 100, 200, { jobs: [] })
  const child = pointRun('other', 300, 400, { workflowId: 'other-workflow', runId: 'other', parentExecutionId: 'root', jobs: [] })
  const rows = [{ runs: [parent] }, { runs: [child] }]
  assert.equal(timelineLayout(rows, [parent, child], 0, 1000, 1000, 800).connections.length, 1)
  const unrelated = { ...child, parentExecutionId: null }
  assert.equal(timelineLayout([{ runs: [parent] }, { runs: [unrelated] }], [parent, unrelated], 0, 1000, 1000, 800).connections.length, 0)
})
