import test from 'node:test'
import assert from 'node:assert/strict'
import { lastRunDay } from './time.js'

test('last run uses Stockholm dates and preserves older dates', () => {
  const now = Date.parse('2026-11-13T23:30:00Z') // 14 November in Stockholm
  assert.deepEqual(lastRunDay('2026-11-13T23:01:00Z', now), { label: 'Today', tone: 'today' })
  assert.deepEqual(lastRunDay('2026-11-13T22:59:00Z', now), { label: 'Yesterday', tone: 'yesterday' })
  assert.deepEqual(lastRunDay('2026-11-12T12:00:00Z', now), { label: '12 Nov 2026', tone: '' })
  assert.equal(lastRunDay(null, now).label, '—')
  assert.equal(lastRunDay('invalid', now).label, '—')
})

test('yesterday follows calendar days across DST and year boundaries', () => {
  assert.equal(lastRunDay('2026-10-24T22:30:00Z', Date.parse('2026-10-25T23:30:00Z')).label, 'Yesterday')
  assert.equal(lastRunDay('2026-03-28T12:00:00Z', Date.parse('2026-03-29T12:00:00Z')).label, 'Yesterday')
  assert.equal(lastRunDay('2026-12-31T12:00:00Z', Date.parse('2027-01-01T12:00:00Z')).label, 'Yesterday')
})
