import { test, expect } from '@playwright/test'

const prefix = '/api/orchestration/task-engine'
const wf = '11111111-1111-4111-8111-111111111111'
const child = '22222222-2222-4222-8222-222222222222'
const dbt = '33333333-3333-4333-8333-333333333333'
const job = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const execution = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const stamp = '2026-11-23T14:00:00Z'

function row(id, name, type = 'SQL') {
  return { WORKFLOW_ID: id, WORKFLOW_NAME: name, WORKFLOW_GROUP: 'DEV', IS_ENABLED: true, IS_SCHEDULE_ENABLED: false, JOB_EXECUTION_MODE: 'SEQUENTIAL',
    source: 'POSTGRES', run: { status: 'NOT RUN', runId: null, start: null, end: null, message: '' }, lastRun: null,
    jobs: [{ JOB_ID: id === wf ? job : id, JOB_NAME: `${name} job`, JOB_TYPE: type, JOB_LAYER: 'GENERAL', JOB_SEQUENCE: 1, JOB_IS_ENABLED: true,
      run: { status: 'NOT RUN', runId: null, start: null, end: null, message: '' }, lastRun: null, progress: null }],
    progress: { total: 0, succeeded: 0, failed: 0, skipped: 0, percent: 0 }, canRun: id === wf,
    runUnavailable: id === child ? 'Start the complete dependency group from WF1' : id === dbt ? 'DBT execution is not enabled in this worker' : '',
    graphId: 'graph', runScope: ['WF1', 'WF2'], configurationRevision: 1, canCancel: false, queuedCount: 0 }
}

async function setup(page, { started = false } = {}) {
  await page.clock.install({ time: new Date(stamp) })
  await page.addInitScript(() => {
    window.__eventUrls = []
    window.EventSource = class { constructor(url) { window.__eventUrls.push(url) } addEventListener() {} close() {} }
  })
  const orphan = row('unassigned', 'Unassigned jobs')
  orphan.isUnassigned = true; orphan.IS_ENABLED = false; orphan.canRun = false; orphan.jobs[0].JOB_NAME = 'Unassigned SQL'
  orphan.jobs[0].runOnly = true
  const state = { data: { ok: true, source: 'POSTGRES', rows: [row(wf, 'WF1'), row(child, 'WF2'), row(dbt, 'DBT workflow', 'DBT'), orphan],
    workflowsCount: 3, jobsCount: 4, unassignedJobs: 1, runsEnabled: true, refreshSeconds: 2, generatedAt: stamp, configurationRevision: 1,
    submissions: [], warnings: [], worker: { state: 'HEALTHY' }, scheduleMessage: 'Imported schedules are not enabled in the PostgreSQL engine' },
    requests: [], writes: [], errors: [], fail: false, rejectStart: false, conflict: false }
  const completed = { runId: execution, executionId: execution, workflowId: wf, workflowName: 'WF1', workflowGroup: 'DEV', source: 'POSTGRES',
    status: 'SUCCESS', start: '2026-11-23T13:58:00Z', end: '2026-11-23T13:59:00Z', requestedAt: '2026-11-23T13:57:00Z', requestedBy: 'Tester', mode: 'WORKFLOW',
    jobs: [{ jobId: job, executionId: 'job-execution', runId: execution, name: 'WF1 job', type: 'SQL', status: 'SUCCESS', start: '2026-11-23T13:58:00Z', end: '2026-11-23T13:59:00Z', position: 1, selected: true }] }
  if (started) {
    state.data.rows[0].run = completed; state.data.rows[0].lastRun = completed
    state.data.rows[0].jobs[0].run = completed.jobs[0]; state.data.rows[0].jobs[0].lastRun = completed.jobs[0]
    state.data.rows[0].progress = { total: 1, succeeded: 1, failed: 0, skipped: 0, completed: 1, percent: 100 }
  }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    state.requests.push(path)
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', taskPrototypeEnabled: true, legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === prefix + '/monitor') {
      if (state.fail) return route.fulfill({ status: 503, json: { error: 'PostgreSQL status unavailable' } })
      body = state.data
    }
    if (req.method() !== 'GET') state.writes.push({ path, body: req.postData() ? req.postDataJSON() : null })
    if (path === prefix + '/graphs/graph/runs') {
      if (state.rejectStart) return route.fulfill({ status: 503, json: { error: 'Admission response unavailable' } })
      const root = state.data.rows[0], waiting = state.data.rows[1]
      root.run = { status: 'QUEUED', runId: execution, requestedAt: stamp, message: '' }; root.canRun = false; root.canCancel = true; root.cascadeId = 'cascade'
      waiting.run.status = 'WAITING'; waiting.run.message = 'WF1: Waiting for parent completion'
      body = { ok: true, cascadeId: 'cascade' }
    }
    if (path === prefix + '/cascades/cascade/cancel') {
      state.data.rows[0].cancelRequested = true; state.data.rows[0].run.message = 'Cancellation requested; awaiting task confirmation'
    }
    if (path === prefix + '/configuration/workflows/' + wf) {
      if (state.conflict) return route.fulfill({ status: 409, json: { error: 'Configuration changed in another editor' } })
      state.data.rows[0].IS_ENABLED = req.postDataJSON().enabled; state.data.configurationRevision++
    }
    if (path === prefix + '/history') body = { ok: true, source: 'POSTGRES', generatedAt: stamp, runs: [completed], nextCursor: null }
    if (path === `${prefix}/workflows/${wf}/runs/${execution}/log`) body = { ok: true, source: 'POSTGRES', history: { WORKFLOW_NAME: 'WF1', STATUS: 'SUCCESS', REQUESTED_BY: 'Tester' },
      jobExecutions: [{ JOB_NAME: 'WF1 job', STATUS: 'SUCCESS', QUERY_ID: 'native-query', TASK_CLEANUP: 'DONE' }], runLog: [], modelProgress: [], testProgress: [], warnings: {} }
    return route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationMonitor')
  await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeVisible()
  return state
}

function pgOnly(state) {
  expect(state.requests.filter(path => path.startsWith('/api/orchestration/') && !path.startsWith(prefix))).toEqual([])
  expect(state.errors).toEqual([])
}

test('existing Monitor shows every catalog job and uses PostgreSQL presence without Snowflake readers', async ({ page }) => {
  const state = await setup(page)
  await expect(page.locator('.om-notice')).toContainText('3 workflows · 4 jobs · 1 unassigned')
  await page.getByRole('button', { name: 'Expand all', exact: true }).click()
  await expect(page.getByRole('table')).toContainText('Unassigned SQL')
  await expect(page.getByRole('table')).toContainText('DBT execution is not enabled')
  await expect(page.getByRole('button', { name: 'Run workflow DBT workflow', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Run WF1 job in WF1', exact: true })).toBeDisabled()
  await expect(page.getByRole('region', { name: 'Workflow dependency trial' })).toHaveCount(0)
  expect((await page.evaluate(() => window.__eventUrls)).every(url => url.includes('orchestrationSource=POSTGRES'))).toBe(true)
  pgOnly(state)
})

test('root Run starts the full dependency group once and shows queued and waiting states', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Run the full dependency group')
  await expect(page.getByRole('dialog')).toContainText('WF1, WF2')
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.writes[0]).toMatchObject({ path: prefix + '/graphs/graph/runs', body: { workflowId: wf } })
  expect(Object.keys(state.writes[0].body).sort()).toEqual(['requestId', 'workflowId'])
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeDisabled()
  await expect(page.getByRole('table')).toContainText('WAITING')
  pgOnly(state)
})

test('ambiguous admission retry retains request identity', async ({ page }) => {
  const state = await setup(page)
  state.rejectStart = true
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Admission response unavailable')
  state.rejectStart = false
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.writes[0].body.requestId).toBe(state.writes[1].body.requestId)
  pgOnly(state)
})

test('state controls use the catalog revision and conflict cannot change enabled state', async ({ page }) => {
  const state = await setup(page)
  state.conflict = true
  await page.getByRole('button', { name: 'Actions for workflow WF1', exact: true }).click()
  await page.getByRole('button', { name: 'Disable workflow', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Configuration changed')
  expect(state.data.rows[0].IS_ENABLED).toBe(true)
  expect(state.writes[0]).toMatchObject({ path: `${prefix}/configuration/workflows/${wf}`, body: { revision: 1, enabled: false } })
  pgOnly(state)
})

test('cancel requests the execution group and waits for confirmation', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: 'Actions for workflow WF1', exact: true }).click()
  await page.getByRole('button', { name: 'Cancel execution group', exact: true }).click()
  await expect(page.getByRole('table')).toContainText('Cancellation requested; awaiting task confirmation')
  expect(state.data.rows[0].run.status).toBe('QUEUED')
  expect(state.writes.at(-1).path).toBe(prefix + '/cascades/cascade/cancel')
  pgOnly(state)
})

test('history and execution log follow the PostgreSQL execution identity', async ({ page }) => {
  const state = await setup(page, { started: true })
  await page.getByRole('button', { name: 'Execution history for WF1', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Execution History', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Log for WF1', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'WF1', exact: true })).toBeVisible()
  await expect(page.getByRole('table')).toContainText('native-query')
  expect(state.requests).toContain(`${prefix}/workflows/${wf}/runs/${execution}/log`)
  pgOnly(state)
})

test('timeline reads PostgreSQL history and unavailable status retains the previous rows', async ({ page }) => {
  const state = await setup(page, { started: true })
  await page.getByRole('button', { name: 'Timeline', exact: true }).click()
  await expect.poll(() => state.requests.includes(prefix + '/history')).toBe(true)
  await page.getByRole('button', { name: 'Table', exact: true }).click()
  state.fail = true
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(page.getByRole('alert')).toContainText('PostgreSQL status unavailable')
  await expect(page.getByRole('table')).toContainText('WF1')
  await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
  pgOnly(state)
})

test('missing feature discovery never selects Snowflake or opens a presence stream', async ({ page }) => {
  const requests = []
  await page.addInitScript(() => { window.__eventUrls = []; window.EventSource = class { constructor(url) { window.__eventUrls.push(url) } addEventListener() {} close() {} } })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    requests.push(path)
    return route.fulfill({ status: path === '/api/health' ? 503 : 200, json: path === '/api/health' ? { error: 'Unavailable' } : { ok: true } })
  })
  await page.goto('/?page=orchestrationMonitor')
  await expect(page.getByRole('alert')).toContainText('Could not determine the configuration source')
  expect(requests.filter(path => path.startsWith('/api/orchestration/'))).toEqual([])
  expect(await page.evaluate(() => window.__eventUrls)).toEqual([])
})
