import { test, expect } from '@playwright/test'

async function setup(page, { failCreate = false } = {}) {
  await page.clock.install({ time: new Date('2026-11-23T13:00:00Z') })
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const state = { schedules: [], occurrences: [], mutations: [], scheduler: 'HEALTHY', failPatch: false }
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', taskPrototypeEnabled: true, legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/orchestration/monitor') body = { ok: true, rows: [], submissions: [], runsEnabled: true }
    if (path === '/api/orchestration/task-engine') body = { ok: true, counts: {}, runs: [], cleanupRetrying: 0 }
    if (path.startsWith('/api/orchestration/task-engine/schedules')) {
      if (req.method() === 'GET') body = { ok: true, schedules: state.schedules, occurrences: state.occurrences,
        enabledCount: state.schedules.filter(s => s.enabled).length, scheduler: { state: state.scheduler, lastSeen: '2026-11-23T13:00:00Z' } }
      if (req.method() === 'POST') {
        const submitted = req.postDataJSON()
        state.mutations.push({ method: req.method(), body: submitted })
        if (failCreate && state.mutations.length === 1) return route.fulfill({ status: 503, json: { error: 'Schedule response unavailable' } })
        const row = { ...submitted, id: 'schedule-1', revision: 1, state: 'PAUSED', grace_seconds: submitted.graceSeconds, next_fire_at: null }
        state.schedules = [row]; body = { ok: true, schedule: row }
      }
      if (req.method() === 'PATCH') {
        const submitted = req.postDataJSON()
        state.mutations.push({ method: req.method(), body: submitted })
        if (state.failPatch) return route.fulfill({ status: 409, json: { error: 'Schedule changed; refresh before saving' } })
        const row = state.schedules[0]
        Object.assign(row, submitted, { revision: row.revision + 1 })
        row.state = row.enabled ? 'ENABLED' : 'PAUSED'
        row.next_fire_at = row.enabled ? '2026-11-23T13:01:00Z' : null
        body = { ok: true, schedule: row }
      }
    }
    if (path === '/api/orchestration/task-engine/runs/run-1') body = { ok: true,
      run: { id: 'run-1', state: 'RUNNING' }, jobs: [{ id: 'job-1', job_key: 'scheduled', state: 'RUNNING', cleanup_state: 'NONE' }] }
    return route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationMonitor')
  await expect(page.getByRole('region', { name: 'Trial schedules' })).toBeVisible()
  return state
}

async function create(page) {
  const form = page.getByRole('form', { name: 'Create trial schedule' })
  await form.getByLabel('Schedule name').fill('Weekday trial')
  await form.getByLabel('Schedule cron').fill('0 7 * * 1-5')
  await form.getByRole('button', { name: 'Create paused schedule' }).click()
}

test('create is paused, enabling uses revision and schedule state is separate from scheduler health', async ({ page }) => {
  const state = await setup(page)
  await create(page)
  const table = page.getByRole('table', { name: 'Trial schedule list' })
  await expect(table).toContainText('PAUSED')
  expect(state.mutations).toHaveLength(1)
  expect(state.mutations[0].body.enabled).toBe(false)
  expect(state.mutations[0].body.plan.workflowKey).toBe('trial-success')
  await table.getByRole('button', { name: 'Enable', exact: true }).click()
  await expect(table).toContainText('ENABLED')
  expect(state.mutations[1].body).toEqual({ expectedRevision: 1, enabled: true })
  await expect(table).toContainText('14:01:00')
  state.scheduler = 'UNAVAILABLE'
  await page.clock.runFor(5100)
  await expect(page.locator('.task-trial-scheduler-health')).toContainText('UNAVAILABLE')
  await expect(table).toContainText('ENABLED')
})

test('pause retains active execution and occurrences distinguish misses from outcomes', async ({ page }) => {
  const state = await setup(page)
  await create(page)
  const table = page.getByRole('table', { name: 'Trial schedule list' })
  await table.getByRole('button', { name: 'Enable', exact: true }).click()
  await expect(table).toContainText('ENABLED')
  state.schedules[0].active_run = { id: 'run-1', state: 'RUNNING' }
  state.schedules[0].last_occurrence = { state: 'SKIPPED_OVERLAP', scheduledFor: '2026-11-23T13:00:00Z' }
  state.occurrences = [{ id: 'miss-1', schedule_id: 'schedule-1', state: 'MISSED', scheduled_for: '2026-11-22T13:00:00Z', window_end: '2026-11-23T13:00:00Z', reason: 'Older occurrences coalesced' }]
  await table.getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(table).toContainText('PAUSED')
  await expect(table).toContainText('RUNNING')
  await expect(table).toContainText('SKIPPED_OVERLAP')
  await expect(page.getByRole('table', { name: 'Schedule occurrences' })).toContainText('MISSED')
  await expect(page.getByRole('table', { name: 'Schedule occurrences' })).toContainText('(exclusive)')
  expect(state.mutations).toHaveLength(3)
  await table.getByRole('button', { name: 'View run', exact: true }).click()
  await expect(page.locator('.task-trial > p').filter({ hasText: 'Run:' })).toContainText('run-1')
  await expect(page.getByLabel('Task trial run')).toHaveValue('run-1')
})

test('ambiguous schedule creation retry reuses request ID', async ({ page }) => {
  const state = await setup(page, { failCreate: true })
  await create(page)
  await expect(page.getByRole('region', { name: 'Trial schedules' }).getByRole('alert')).toHaveText('Schedule response unavailable')
  await page.getByRole('button', { name: 'Create paused schedule', exact: true }).click()
  await expect(page.getByRole('table', { name: 'Trial schedule list' })).toContainText('Weekday trial')
  expect(state.mutations[0].body.requestId).toBe(state.mutations[1].body.requestId)
})

test('stale edit displays conflict and preserves the saved schedule', async ({ page }) => {
  const state = await setup(page)
  await create(page)
  const table = page.getByRole('table', { name: 'Trial schedule list' })
  await table.getByRole('button', { name: 'Edit', exact: true }).click()
  const form = page.getByRole('form', { name: 'Edit trial schedule' })
  await form.getByLabel('Schedule cron').fill('0 8 * * 1-5')
  state.failPatch = true
  await form.getByRole('button', { name: 'Save schedule', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Trial schedules' }).getByRole('alert')).toContainText('Schedule changed; refresh before saving')
  await expect(table).toContainText('0 7 * * 1-5')
  expect(state.mutations[1].body.expectedRevision).toBe(1)
})
