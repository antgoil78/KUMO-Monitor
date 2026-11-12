import test from 'node:test'
import assert from 'node:assert/strict'
import { refreshTimelineHistory, retainedHistoryBoundary } from './orchestrationTimelineHistory.js'

const run = (id, day) => ({ workflowId: 'wf', runId: id, executionId: id, requestedAt: `2026-11-${day}T12:00:00Z`, start: `2026-11-${day}T12:00:00Z`, end: `2026-11-${day}T12:01:00Z`, status: 'SUCCESS' })
const query = { start: '2026-11-04T12:00:00Z', end: '2026-11-11T12:00:00Z', limit: 100 }

test('initial history reads only one bounded page', async () => {
  const calls = []
  const result = await refreshTimelineHistory(async (q, force) => {
    calls.push([q, force]); return { runs: [run('first', '11')], nextCursor: 'next' }
  }, query, null, true, () => true)
  assert.deepEqual(calls, [[query, true]])
  assert.equal(result.pageCount, 1)
  assert.equal(result.nextCursor, 'next')
})

test('refresh follows new cursors past the loaded page count to retain older runs without gaps', async () => {
  const previous = { pageCount: 2, runs: [run('before', '10'), run('older', '07')] }
  const pages = [
    { runs: [run('new', '11')], nextCursor: 'a' },
    { runs: [run('before', '10')], nextCursor: 'b' },
    { runs: [run('middle', '09')], nextCursor: 'c' },
    { runs: [run('older', '07')], nextCursor: 'd' }
  ]
  const calls = []
  const result = await refreshTimelineHistory(async q => { calls.push(q); return pages.shift() }, query, previous, false, () => true)
  assert.deepEqual(calls.map(q => q.cursor), [undefined, 'a', 'b', 'c'])
  assert.deepEqual(result.runs.map(r => r.runId), ['new', 'before', 'middle', 'older'])
  assert.equal(result.nextCursor, 'd')
  assert.equal(result.pageCount, 4)
})

test('range boundary excludes expired runs and retains the exact root at timestamp ties', async () => {
  const previous = { pageCount: 2, runs: [run('z', '07'), run('a', '07'), run('expired', '01')] }
  assert.equal(retainedHistoryBoundary(previous, query), 'wf:a')
  const pages = [{ runs: [run('z', '07')], nextCursor: 'a' }, { runs: [run('a', '07')], nextCursor: 'b' }]
  const result = await refreshTimelineHistory(async () => pages.shift(), query, previous, false, () => true)
  assert.equal(result.pageCount, 2)
  assert.equal(result.nextCursor, 'b')
})

test('replay stops at the actual end and deduplicates execution roots', async () => {
  const previous = { pageCount: 3, runs: [run('deleted', '05')] }
  const pages = [{ runs: [run('a', '10')], nextCursor: 'a' }, { runs: [run('a', '10')], nextCursor: null }]
  const result = await refreshTimelineHistory(async () => pages.shift(), query, previous, false, () => true)
  assert.equal(result.runs.length, 1)
  assert.equal(result.nextCursor, null)
})

test('cancelled refresh cannot publish results or request subsequent pages', async () => {
  let active = true, calls = 0
  const result = await refreshTimelineHistory(async () => {
    calls++; active = false; return { runs: [run('a', '10')], nextCursor: 'a' }
  }, query, { pageCount: 2, runs: [] }, false, () => active)
  assert.equal(result, null)
  assert.equal(calls, 1)
})

test('failed or looping history pages reject the refresh instead of reporting incomplete coverage', async () => {
  await assert.rejects(refreshTimelineHistory(async () => { throw new Error('unavailable') }, query, null, false, () => true), /unavailable/)
  await assert.rejects(refreshTimelineHistory(async () => ({ runs: [], nextCursor: 'same' }), query, { pageCount: 3, runs: [] }, false, () => true), /repeated page cursor/)
})
