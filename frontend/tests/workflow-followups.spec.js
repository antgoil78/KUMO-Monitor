import { test, expect } from '@playwright/test'

test('dashboard, relative run dates and manual dbt full refresh use the workflow hierarchy', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-11-13T12:00:00Z') })
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const errors = [], submissions = []
  page.on('pageerror', error => errors.push(error.message))
  const workflowId = '00000000-0000-0000-0000-000000000001'
  const dbtId = '00000000-0000-0000-0000-000000000002'
  const run = { status: 'SUCCESS', runId: 'previous', start: '2026-11-13T08:00:00Z', end: '2026-11-13T08:01:00Z' }
  const monitor = { ok: true, runsEnabled: true, rows: [{ WORKFLOW_ID: workflowId, WORKFLOW_NAME: 'PYMT LOAD', WORKFLOW_GROUP: 'LOAD', IS_ENABLED: true,
    IS_SCHEDULE_ENABLED: false, ALLOW_MANUAL_TRIGGER: true, canRun: true, run, lastRun: run,
    progress: { total: 2, completed: 2, percent: 100 }, jobs: [
      { JOB_ID: dbtId, JOB_NAME: 'PYMT SDL', JOB_TYPE: 'DBT', JOB_IS_ENABLED: true, JOB_SEQUENCE: 1, run, lastRun: run },
      { JOB_ID: 'sql', JOB_NAME: 'PYMT READY', JOB_TYPE: 'SQL', JOB_IS_ENABLED: true, JOB_SEQUENCE: 2, run, lastRun: { ...run, start: '2026-11-12T08:00:00Z' } }
    ] }], submissions: [], warnings: [] }
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    let body = { ok: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/health') body = { ok: true, legacyEngineRetired: true, environment: 'DEV' }
    if (path === '/api/dashboard') body = { ok: true, monitor, health: { ok: true }, ping: { ok: true } }
    if (path === '/api/dashboard/lim-attention') body = { summary: {}, attention: [] }
    if (path === '/api/orchestration/monitor') body = monitor
    if (request.method() === 'POST' && path.endsWith('/runs')) {
      submissions.push(request.postDataJSON())
      body = { ok: true, accepted: true, workflowId, ...request.postDataJSON(), status: 'INITIATING', queryId: 'query' }
    }
    return route.fulfill({ json: body })
  })
  await page.goto('/?page=dashboard')
  await expect(page.locator('.metric-card').first()).toContainText('1')
  await expect(page.locator('.metric-card').first()).toContainText('2 assigned jobs · 2 enabled')
  await expect(page.locator('.health-list')).toContainText('READY')
  await expect(page.locator('.activity-list')).toContainText('PYMT LOAD')

  await page.goto('/?page=orchestrationMonitor')
  await page.getByRole('button', { name: 'Expand all', exact: true }).click()
  await expect(page.locator('.om-day-today')).toHaveCount(2)
  await expect(page.locator('.om-day-today').first()).toHaveCSS('color', 'rgb(114, 180, 255)')
  await expect(page.locator('.om-day-yesterday')).toHaveText('Yesterday')
  await expect(page.locator('.om-day-yesterday')).toHaveCSS('color', 'rgb(244, 204, 99)')
  await page.getByRole('button', { name: 'Run PYMT READY in PYMT LOAD', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: /Full refresh/ })).toHaveCount(0)
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('button', { name: 'Run workflow PYMT LOAD', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: /Full refresh/ })).toHaveCount(0)
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('button', { name: 'Run PYMT SDL in PYMT LOAD', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: /Full refresh/ })).not.toBeChecked()
  await page.getByRole('checkbox', { name: /Full refresh/ }).check()
  await page.getByLabel('Run mode', { exact: true }).selectOption('FROM_JOB')
  await page.getByRole('button', { name: 'Run from job', exact: true }).click()
  await expect.poll(() => submissions.length).toBe(1)
  expect(submissions[0]).toMatchObject({ mode: 'FROM_JOB', jobId: dbtId, fullRefresh: true })
  expect(errors).toEqual([])
})
