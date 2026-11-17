import { test, expect } from '@playwright/test'

test('workflow-wide execution mode saves, reloads and appears in the run dialog', async ({ page }) => {
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const errors = [], saved = []
  page.on('pageerror', error => errors.push(error.message))
  const workflow = { WORKFLOW_ID: 'wf', WORKFLOW_NAME: 'Execution choice', IS_ENABLED: true,
    ALLOW_MANUAL_TRIGGER: true, JOB_EXECUTION_MODE: 'SEQUENTIAL' }
  const jobs = [1, 2].map(index => ({ JOB_ID: `job-${index}`, JOB_NAME: `Job ${index}`, JOB_TYPE: 'SQL',
    IS_ENABLED: true, SQL_COMMAND: 'SELECT 1' }))
  const assignments = jobs.map((job, index) => ({ ...job, WORKFLOW_ID: 'wf', JOB_SEQUENCE: index + 1,
    PARENT_WORKFLOW_ID: index === 0 ? 'wf' : null, PARENT_JOB_ID: index === 0 ? null : 'job-1' }))
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/orchestration/monitor') body = { ok: true, rows: [], warnings: [], submissions: [] }
    if (path === '/api/orchestration/engine') body = { ok: true, installed: true, runsEnabled: true }
    if (path === '/api/orchestration/configuration') body = { ok: true, workflows: [workflow], jobs,
      assignments, rows: [{ id: 'wf', workflowId: 'wf', parentId: null }], warnings: [], invalidWorkflows: [], revision: 'v1' }
    if (path === '/api/orchestration/workflows/wf' && request.method() === 'PATCH') {
      saved.push(request.postDataJSON())
      workflow.JOB_EXECUTION_MODE = saved.at(-1).jobExecutionMode
    }
    await route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationWorkflow')
  await page.getByRole('button', { name: 'Edit workflow Execution choice', exact: true }).click()
  await expect(page.getByLabel('Job execution', { exact: true })).toHaveValue('SEQUENTIAL')
  await page.getByLabel('Job execution', { exact: true }).selectOption('PARALLEL')
  await page.getByRole('button', { name: 'Save workflow', exact: true }).click()
  await expect.poll(() => saved.length).toBe(1)
  expect(saved[0].jobExecutionMode).toBe('PARALLEL')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: 'Edit workflow Execution choice', exact: true }).click()
  await expect(page.getByLabel('Job execution', { exact: true })).toHaveValue('PARALLEL')
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('button', { name: 'Run workflow Execution choice', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Run all enabled jobs in parallel.')
  await expect(page.getByRole('dialog')).toContainText('Selected jobs start together and run independently.')
  expect(errors).toEqual([])
})
