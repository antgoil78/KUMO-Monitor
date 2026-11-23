import { test, expect } from '@playwright/test'

const prefix = '/api/orchestration/task-engine/configuration'

function configuration() {
  const base = { jobId: null, parentId: null, trigger: null, success: null, warning: null, error: null, sql: '', allow: true, parentMustBeNewer: false }
  const workflows = ['WF1', 'WF2', 'WF3', 'WF4'].map(key => ({ WORKFLOW_ID: key, WORKFLOW_NAME: key, WORKFLOW_GROUP: 'DEPENDENCY TEST',
    IS_ENABLED: true, ALLOW_MANUAL_TRIGGER: true, JOB_EXECUTION_MODE: 'SEQUENTIAL' }))
  const jobs = workflows.map(w => ({ JOB_ID: `${w.WORKFLOW_ID}-sql`, JOB_NAME: `${w.WORKFLOW_NAME} SQL`, JOB_TYPE: 'SQL', JOB_LAYER: 'GENERAL',
    IS_ENABLED: true, SQL_COMMAND: 'SELECT 1', TIMEOUT_SECONDS: 180 }))
  const assignments = workflows.map((w, i) => ({ WORKFLOW_ID: w.WORKFLOW_ID, JOB_ID: jobs[i].JOB_ID, JOB_SEQUENCE: 1, PARENT_WORKFLOW_ID: w.WORKFLOW_ID }))
  const rows = workflows.flatMap((w, i) => [
    { ...base, id: w.WORKFLOW_ID, workflowId: w.WORKFLOW_ID, name: w.WORKFLOW_NAME,
      ...(i ? { parentId: i === 3 ? 'WF2' : 'WF1', trigger: 'DEP', success: true, warning: false, error: false, parentMustBeNewer: true } : {}) },
    { ...base, id: `${w.WORKFLOW_ID}:job:${jobs[i].JOB_ID}`, workflowId: w.WORKFLOW_ID, jobId: jobs[i].JOB_ID, name: jobs[i].JOB_NAME, parentId: w.WORKFLOW_ID, trigger: 'DEP' }
  ])
  rows.push({ ...base, id: 'WF4:parent:WF3', workflowId: 'WF4', name: 'WF4', parentId: 'WF3', trigger: 'DEP', success: true, warning: false, error: false })
  return { ok: true, source: 'PostgreSQL · WF1–WF4 DEV trial', configurationStore: 'POSTGRES', revision: 1,
    workflows, jobs, assignments, rows, warnings: [], invalidWorkflows: [], revisions: [{ revision: 1, changed_by: 'IMPORTER', changed_at: '2026-11-23T13:00:00Z' }] }
}

async function setup(page, target = 'orchestrationWorkflow') {
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const state = { data: configuration(), requests: [], mutations: [], errors: [], fail: false, conflict: false }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    state.requests.push(path)
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', taskPrototypeEnabled: true, legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path.startsWith(prefix)) {
      if (state.fail) return route.fulfill({ status: 503, json: { error: 'Prototype database unavailable' } })
      if (req.method() !== 'GET') {
        const payload = req.postDataJSON()
        state.mutations.push({ path, payload })
        if (state.conflict) return route.fulfill({ status: 409, json: { error: 'Configuration changed in another editor. Close this dialog and refresh before saving again.' } })
        if (path === prefix) state.data.rows = payload.rows
        if (path.includes('/workflows/')) {
          const w = state.data.workflows.find(w => w.WORKFLOW_ID === path.split('/').at(-1))
          Object.assign(w, { WORKFLOW_NAME: payload.workflowName, JOB_EXECUTION_MODE: payload.jobExecutionMode, IS_ENABLED: payload.enabled })
        }
        if (path.includes('/jobs/')) {
          const j = state.data.jobs.find(j => j.JOB_ID === path.split('/').at(-1))
          Object.assign(j, { JOB_NAME: payload.jobName, SQL_COMMAND: payload.sqlCommand, IS_ENABLED: payload.enabled, TIMEOUT_SECONDS: Number(payload.timeoutSeconds) })
        }
        state.data.revision++
      }
      body = state.data
    }
    if (path === '/api/orchestration/configuration') body = { ...configuration(), source: 'Snowflake existing catalog', revision: 'sf-1' }
    if (path === '/api/orchestration/engine') body = { ok: true, installed: true, runsEnabled: true }
    if (path === '/api/orchestration/monitor') body = { ok: true, rows: [], submissions: [] }
    return route.fulfill({ json: body })
  })
  await page.goto(`/?page=${target}`)
  if (!state.fail) await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
  return state
}

function expectPostgresOnly(state) {
  expect(state.requests.filter(path => path.startsWith('/api/orchestration/') && !path.startsWith(prefix))).toEqual([])
  expect(state.mutations.every(m => m.payload.revision >= 1)).toBe(true)
  expect(state.errors).toEqual([])
}

test('existing workflow editor saves master settings with revision and survives reload', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Edit workflow WF2', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Workflow name', { exact: true }).fill('WF2 revised')
  await dialog.getByLabel('Job execution', { exact: true }).selectOption('PARALLEL')
  await dialog.getByLabel('Workflow enabled', { exact: true }).uncheck()
  await expect(dialog.getByLabel('Schedule enabled')).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Delete', exact: true })).toHaveCount(0)
  await dialog.getByRole('button', { name: 'Save workflow' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.mutations[0].payload).toMatchObject({ revision: 1, workflowName: 'WF2 revised', jobExecutionMode: 'PARALLEL', enabled: false })
  await page.reload()
  await page.getByRole('button', { name: 'Edit workflow WF2 revised', exact: true }).click()
  await expect(page.getByLabel('Job execution', { exact: true })).toHaveValue('PARALLEL')
  await expect(page.getByLabel('Workflow enabled', { exact: true })).not.toBeChecked()
  expectPostgresOnly(state)
})

test('SQL editor saves SQL timeout and disabled state without Snowflake command preview', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Show jobs for WF2', exact: true }).click()
  await page.getByRole('button', { name: 'Edit job WF2 SQL', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByLabel('Job type', { exact: true })).toBeDisabled()
  await dialog.getByLabel('SQL command', { exact: true }).fill('SELECT 42')
  await dialog.locator('input[type=number]').fill('45')
  await dialog.getByLabel('Job enabled', { exact: true }).uncheck()
  await dialog.getByRole('button', { name: 'Save job', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.mutations[0]).toMatchObject({ path: `${prefix}/jobs/WF2-sql`, payload: { revision: 1, sqlCommand: 'SELECT 42', timeoutSeconds: '45', enabled: false } })
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeDisabled()
  expectPostgresOnly(state)
})

test('dependency editor preserves jobs and independently saves parent freshness and outcome rules', async ({ page }) => {
  const state = await setup(page, 'orchestrationDependencies')
  const assignments = structuredClone(state.data.rows.filter(row => row.jobId))
  await page.getByRole('button', { name: 'Expand all', exact: true }).click()
  await page.getByLabel('WF4 parent must be newer · WF2', { exact: true }).uncheck()
  await page.getByLabel('WF2 error rule', { exact: true }).selectOption('true')
  await expect(page.getByLabel('WF2 trigger', { exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Edit SQL for WF2 parent WF1', exact: true })).toBeDisabled()
  await expect(page.getByLabel('Configuration source', { exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Save changes', exact: true }).click()
  await expect(page.locator('.od-message')).toContainText('saved to PostgreSQL')
  const payload = state.mutations[0].payload
  expect(payload.rows.find(row => row.id === 'WF4').parentMustBeNewer).toBe(false)
  expect(payload.rows.find(row => row.id === 'WF2').error).toBe(true)
  expect(payload.rows.filter(row => row.jobId)).toEqual(assignments)
  expectPostgresOnly(state)
})

test('revision conflicts retain the draft and require refresh before resaving', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Edit workflow WF2', exact: true }).click()
  await page.getByLabel('Workflow name', { exact: true }).fill('My draft')
  state.conflict = true
  await page.getByRole('button', { name: 'Save workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Configuration changed in another editor')
  await expect(page.getByLabel('Workflow name', { exact: true })).toHaveValue('My draft')
  await expect(page.getByLabel('Configuration source', { exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  state.data.revision = 2; state.conflict = false
  await page.getByRole('button', { name: 'Refresh', exact: false }).click()
  await page.getByRole('button', { name: 'Edit workflow WF2', exact: true }).click()
  await page.getByRole('button', { name: 'Save workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.mutations.at(-1).payload.revision).toBe(2)
  expectPostgresOnly(state)
})

test('PostgreSQL failure keeps the selected store and never falls back to Snowflake', async ({ page }) => {
  const state = await setup(page, 'orchestrationDependencies')
  state.fail = true
  await page.getByRole('button', { name: 'Refresh', exact: false }).click()
  await expect(page.getByRole('alert')).toContainText('Prototype database unavailable')
  await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
  expectPostgresOnly(state)
})

test('source selection explicitly separates existing Snowflake configuration and persists across reload', async ({ page }) => {
  const state = await setup(page, 'orchestrationDependencies')
  await page.getByLabel('Configuration source', { exact: true }).selectOption('SNOWFLAKE')
  await expect(page.locator('.od-notice')).toContainText('Snowflake existing catalog')
  await page.reload()
  await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('SNOWFLAKE')
  await expect(page.locator('.od-notice')).toContainText('Snowflake existing catalog')
  expect(state.mutations).toEqual([])
  expect(state.errors).toEqual([])
})

test('existing Job page uses the same PostgreSQL master and revision', async ({ page }) => {
  const state = await setup(page, 'orchestrationJob')
  await page.getByRole('button', { name: /GENERAL/ }).click()
  await page.getByRole('row').filter({ hasText: 'WF2 SQL' }).getByRole('button', { name: 'Edit', exact: true }).click()
  await page.getByLabel('Job name', { exact: true }).fill('Updated SQL')
  await page.getByRole('button', { name: 'Save job', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.mutations[0]).toMatchObject({ path: `${prefix}/jobs/WF2-sql`, payload: { revision: 1, jobName: 'Updated SQL' } })
  expectPostgresOnly(state)
})
