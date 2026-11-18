import { test, expect } from '@playwright/test'

const base = { jobId: null, trigger: null, parentId: null, success: null, warning: null, error: null, sql: '', allow: true, parentMustBeNewer: false, issue: '' }
const initialRows = [
  { ...base, id: 'wf1', workflowId: 'wf1', name: 'Parent A' },
  { ...base, id: 'wf2', workflowId: 'wf2', name: 'Parent B' },
  { ...base, id: 'wf3', workflowId: 'wf3', name: 'Child' },
  { ...base, id: 'job3', workflowId: 'wf3', name: 'Assigned job', jobId: 'job', parentId: 'wf3', trigger: 'DEP' }
]

async function setup(page) {
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const state = { submissions: [], errors: [], rejectSave: false }
  let configuration = { ok: true, source: 'DEV', rows: structuredClone(initialRows), jobs: [], workflows: [], revision: 'r0' }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/orchestration/configuration') {
      if (request.method() === 'PUT') {
        if (state.rejectSave) return route.fulfill({ status: 409, json: { ok: false, error: 'Configuration changed since loading.' } })
        state.submissions.push(request.postDataJSON())
        configuration = { ...configuration, rows: state.submissions.at(-1).rows, revision: `r${state.submissions.length}` }
      }
      body = configuration
    }
    await route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationDependencies')
  await expect(page.locator('.od-workflow-header')).toHaveCount(3)
  return state
}

async function addParent(page, parentId) {
  await page.getByRole('button', { name: 'Add parent for Child', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Add parent · Child', exact: true })
  await dialog.getByLabel('Parent workflow', { exact: true }).selectOption(parentId)
  return dialog
}

test('add parents with all conditions, save, reload, and remove parents while retaining assigned jobs', async ({ page }) => {
  const state = await setup(page)
  await expect(page.getByRole('button', { name: 'Show parents for Child' })).toHaveAttribute('aria-expanded', 'false')
  let dialog = await addParent(page, 'wf1')
  await expect(dialog.getByLabel('Parent workflow')).not.toContainText('Child')
  await dialog.getByRole('combobox', { name: 'Child warning rule' }).selectOption('')
  await dialog.getByLabel('SQL condition', { exact: true }).fill('SELECT TRUE')
  await dialog.getByLabel('Allow', { exact: true }).selectOption('false')
  await dialog.getByRole('button', { name: 'Add parent', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Hide parents for Child' })).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('button', { name: 'Remove Parent A parent from Child' })).toBeVisible()
  expect(state.submissions).toHaveLength(0)
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.locator('.od-message')).toContainText('saved to Snowflake')
  expect(state.submissions[0].rows.find(row => row.id === 'wf3')).toMatchObject({
    parentId: 'wf1', trigger: 'DEP', success: true, warning: null, error: false, parentMustBeNewer: true, sql: 'SELECT TRUE', allow: false
  })

  dialog = await addParent(page, 'wf2')
  await expect(dialog.getByLabel('Parent workflow')).not.toContainText('Parent A')
  await expect(dialog.getByLabel('Trigger', { exact: true })).toBeDisabled()
  await dialog.getByLabel("Parent must be newer than the child's last start").uncheck()
  await dialog.getByRole('button', { name: 'Add parent', exact: true }).click()
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.locator('.od-message')).toContainText('saved to Snowflake')
  const links = state.submissions[1].rows.filter(row => !row.jobId && row.workflowId === 'wf3')
  expect(links).toHaveLength(2)
  expect(links[1].id).toMatch(/^[0-9a-f-]{36}$/)
  expect(links[1]).toMatchObject({ parentId: 'wf2', sql: '', allow: true, parentMustBeNewer: false })
  await page.screenshot({ path: test.info().outputPath('dependency-parents.png'), fullPage: true })

  await page.reload()
  await page.getByRole('button', { name: 'Show parents for Child' }).click()
  await expect(page.getByRole('button', { name: 'Remove Parent B parent from Child' })).toBeVisible()
  await page.getByRole('button', { name: 'Remove Parent A parent from Child' }).click()
  await expect(page.locator('.od-errors')).toHaveCount(0)
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.locator('.od-message')).toContainText('saved to Snowflake')
  expect(state.submissions[2].revision).toBe('r2')
  expect(state.submissions[2].rows.find(row => row.id === 'wf3')).toMatchObject({ parentId: 'wf2', parentMustBeNewer: false })
  await page.getByRole('button', { name: 'Remove Parent B parent from Child' }).click()
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.locator('.od-message')).toContainText('saved to Snowflake')
  expect(state.submissions[3].rows.find(row => row.id === 'wf3')).toEqual(initialRows[2])
  for (const submission of state.submissions) expect(submission.rows.filter(row => row.jobId)).toEqual([initialRows[3]])
  await expect(page.locator('.od-config-table')).not.toContainText('Assigned job')
  expect(state.errors).toEqual([])
})

test('cancel and discard restore saved rules; failed saves retain the draft and cycles block saving', async ({ page }) => {
  const state = await setup(page)
  let dialog = await addParent(page, 'wf1')
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeDisabled()
  dialog = await addParent(page, 'wf1')
  await dialog.getByRole('button', { name: 'Add parent', exact: true }).click()
  state.rejectSave = true
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Configuration changed')
  await expect(page.getByRole('button', { name: 'Remove Parent A parent from Child' })).toBeVisible()
  await page.getByRole('button', { name: 'Discard changes', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Remove Parent A parent from Child' })).toHaveCount(0)
  dialog = await addParent(page, 'wf1')
  await dialog.getByRole('button', { name: 'Add parent', exact: true }).click()
  await page.getByRole('button', { name: 'Add parent for Parent A', exact: true }).click()
  dialog = page.getByRole('dialog', { name: 'Add parent · Parent A', exact: true })
  await dialog.getByLabel('Parent workflow', { exact: true }).selectOption('wf3')
  await dialog.getByRole('button', { name: 'Add parent', exact: true }).click()
  await expect(page.locator('.od-errors')).toContainText('cycle')
  await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeDisabled()
  expect(state.submissions).toHaveLength(0)
  expect(state.errors).toEqual([])
})
