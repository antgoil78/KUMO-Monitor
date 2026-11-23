import { test, expect } from '@playwright/test'

test('freshness saves independently for each parent while preserving job assignments', async ({ page }) => {
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const errors = [], submissions = []
  page.on('pageerror', error => errors.push(error.message))
  const base = { jobId: null, trigger: null, parentId: null, success: null, warning: null, error: null, sql: '', allow: true, parentMustBeNewer: false }
  const rows = [
    { ...base, id: 'wf2', workflowId: 'wf2', name: 'WF2' },
    { ...base, id: 'wf3', workflowId: 'wf3', name: 'WF3' },
    { ...base, id: 'wf4', workflowId: 'wf4', name: 'WF4', parentId: 'wf2', trigger: 'DEP', parentMustBeNewer: true },
    { ...base, id: 'wf4-rule3', workflowId: 'wf4', name: 'WF4', parentId: 'wf3', trigger: 'DEP', parentMustBeNewer: true },
    { ...base, id: 'job4', workflowId: 'wf4', name: 'Dummy job', jobId: 'job', parentId: 'wf4', trigger: 'DEP' }
  ]
  let configuration = { ok: true, source: 'DEV', rows, jobs: [], workflows: [], revision: 'r1' }
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/orchestration/configuration') {
      if (request.method() === 'PUT') {
        submissions.push(request.postDataJSON())
        configuration = { ...configuration, rows: submissions.at(-1).rows, revision: 'r2' }
      }
      body = configuration
    }
    await route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationDependencies')
  await expect(page.locator('.od-workflow-header')).toHaveCount(3)
  await page.getByRole('button', { name: 'Expand all', exact: true }).click()
  await expect(page.locator('.od-parent-rules tr')).toHaveCount(4)
  await expect(page.locator('.od-config-table')).not.toContainText('Dummy job')
  const wf2Rule = page.getByRole('checkbox', { name: 'WF4 parent must be newer · WF2', exact: true })
  const wf3Rule = page.getByRole('checkbox', { name: 'WF4 parent must be newer · WF3', exact: true })
  await expect(wf2Rule).toBeChecked()
  await expect(wf3Rule).toBeChecked()
  await wf2Rule.uncheck()
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.locator('.od-message')).toContainText('saved to Snowflake')
  expect(submissions).toHaveLength(1)
  expect(submissions[0].rows.find(row => row.id === 'wf4').parentMustBeNewer).toBe(false)
  expect(submissions[0].rows.find(row => row.id === 'wf4-rule3').parentMustBeNewer).toBe(true)
  expect(submissions[0].rows.find(row => row.id === 'job4')).toEqual(rows[4])
  await expect(wf3Rule).toBeChecked()
  expect(errors).toEqual([])
})
