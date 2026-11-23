import { test, expect } from '@playwright/test'

async function trial(page, { enabled = true, failFirst = false } = {}) {
  await page.clock.install({ time: new Date('2026-11-23T12:00:00Z') })
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const state = { reads: 0, submissions: [], run: null, cleanup: 'NONE', status: 'QUEUED', cancel: false }
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', taskPrototypeEnabled: enabled, legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/orchestration/monitor') body = { ok: true, rows: [], submissions: [], runsEnabled: true }
    if (path === '/api/orchestration/task-engine') {
      state.reads++
      body = { ok: true, counts: state.run ? { [state.status]: 1 } : {}, cleanupRetrying: state.cleanup === 'PENDING' ? 1 : 0,
        runs: state.run ? [{ id: 'run-1', request_id: state.run.requestId, state: state.status }] : [] }
    }
    if (path === '/api/orchestration/task-engine/runs' && request.method() === 'POST') {
      state.submissions.push(request.postDataJSON())
      if (failFirst && state.submissions.length === 1) return route.fulfill({ status: 503, json: { error: 'Admission response unavailable' } })
      state.run = request.postDataJSON()
      body = { ok: true, runId: 'run-1' }
    }
    if (path === '/api/orchestration/task-engine/runs/run-1/cancel') state.cancel = true
    if (path.startsWith('/api/orchestration/task-engine/runs/run-1')) body = { ok: true,
      run: { id: 'run-1', state: state.status, cancel_requested: state.cancel },
      jobs: [{ id: 'job-1', job_key: 'first', state: state.status, cleanup_state: state.cleanup }] }
    return route.fulfill({ json: body })
  })
  return state
}

test('trial is opt-in and never requests PostgreSQL routes when disabled', async ({ page }) => {
  const state = await trial(page, { enabled: false })
  await page.goto('/?page=orchestrationMonitor')
  await expect(page.getByText('DEV task engine trial', { exact: true })).toHaveCount(0)
  await page.clock.runFor(10000)
  expect(state.reads).toBe(0)
})

test('terminal runs refresh through cleanup then stop polling', async ({ page }) => {
  const state = await trial(page)
  await page.goto('/?page=orchestrationMonitor')
  const panel = page.locator('.task-trial')
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: 'Run example', exact: true }).click()
  await expect(panel).toContainText('run-1')
  state.status = 'SUCCESS'; state.cleanup = 'PENDING'
  await page.clock.runFor(2100)
  await expect(panel.getByRole('cell', { name: 'PENDING', exact: true })).toBeVisible()
  state.cleanup = 'DONE'
  await page.clock.runFor(2100)
  await expect(panel.getByRole('cell', { name: 'DONE', exact: true })).toBeVisible()
  const reads = state.reads
  await page.clock.runFor(15000)
  expect(state.reads).toBe(reads)
  expect(state.submissions).toHaveLength(1)
})

test('ambiguous submission retry keeps its request ID and displays the error', async ({ page }) => {
  const state = await trial(page, { failFirst: true })
  await page.goto('/?page=orchestrationMonitor')
  const panel = page.locator('.task-trial')
  await panel.getByRole('button', { name: 'Run example', exact: true }).click()
  await expect(panel.getByRole('alert')).toHaveText('Admission response unavailable')
  await panel.getByRole('button', { name: 'Run example', exact: true }).click()
  await expect(panel).toContainText('run-1')
  expect(state.submissions[0].requestId).toBe(state.submissions[1].requestId)
  await panel.getByRole('button', { name: 'Cancel run', exact: true }).click()
  await expect(panel.getByRole('button', { name: 'Cancellation requested', exact: true })).toBeDisabled()
})
