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
    progress: { total: 0, succeeded: 0, failed: 0, skipped: 0, percent: 0 }, canRun: id !== dbt, canRunWithChildren: id !== dbt, scheduleAvailable: id !== dbt,
    runUnavailable: id === dbt ? 'DBT execution is not enabled in this worker' : '',
    graphId: 'graph', runScope: id === wf ? ['WF1', 'WF2'] : ['WF2'], configurationRevision: 1, canCancel: false, queuedCount: 0, waitingCount: 0 }
}

// Model the API's independent execution and planning projections. Tests mutate
// raw request lifecycles without allowing pending requests to erase results.
function monitorRow(workflow) {
  const run = workflow.run
  const actual = workflow.execution || (run.start ? run : workflow.lastRun) || { status: 'NO RUN', runId: null, start: null, end: null, jobs: [] }
  const waitingCount = workflow.waitingCount || (run.status === 'WAITING' ? 1 : 0)
  const scheduled = workflow.IS_SCHEDULE_ENABLED && workflow.IS_ENABLED
  const status = workflow.cancelRequested ? 'CANCELLING' : run.status === 'UNKNOWN' ? 'NEEDS REVIEW' : actual.status === 'RUNNING' ? 'RUNNING' : run.dispatch?.canRetry ? 'BLOCKED' : waitingCount ? 'WAITING'
    : run.status === 'QUEUED' ? 'QUEUED' : run.status === 'BLOCKED' ? 'BLOCKED' : run.status === 'UNKNOWN' ? 'NEEDS REVIEW'
    : scheduled ? 'SCHEDULED' : 'NO PLAN'
  const planning = workflow.planning || { status, waitingCount, queuedCount: workflow.queuedCount,
    message: run.message, waitingFor: run.waitingFor, stopped: run.status === 'BLOCKED',
    scheduled, nextRun: workflow.scheduler?.nextRun, dispatch: run.dispatch, recovery: run.recovery }
  const completedResult = runs => runs.filter(run => run?.start && run?.end && ['SUCCESS', 'WARNING', 'FAILED', 'CANCELLED', 'SKIPPED'].includes(run.status))
    .sort((a, b) => Date.parse(b.end) - Date.parse(a.end))[0] || { status: 'NO RESULT', runId: null, start: null, end: null }
  const lastResult = workflow.lastResult || completedResult([actual, workflow.lastRun, run])
  const jobs = workflow.jobs.map(job => ({ ...job, run: actual.jobs?.find(j => j.jobId === job.JOB_ID)
    || (actual.runId ? job.run : { status: 'NO RUN', runId: null, start: null, end: null, message: '' }),
    lastResult: job.lastResult || completedResult([job.run, job.lastRun, lastResult.jobs?.find(j => j.jobId === job.JOB_ID)]) }))
  const active = ['RUNNING', 'UNKNOWN', 'WAITING', 'QUEUED', 'INITIATING', 'BLOCKED'].includes(run.status) || run.dispatch?.canRetry || workflow.activePlan
  const request = (id, status, includeDetails = true) => ({ ...run, runId: id, requestId: id, status,
    planStatus: workflow.planStatus || 'RUNNING', requestedAt: run.requestedAt || stamp, requestedBy: 'Tester',
    cancelRequested: workflow.cancelRequested, trigger: { kind: 'MANUAL' },
    waitingFor: includeDetails ? run.waitingFor || [] : [], message: includeDetails ? run.message : '',
    jobs: workflow.jobs.map(job => ({ ...job.run, jobId: job.JOB_ID, type: job.JOB_TYPE, source: 'POSTGRES',
      status: status === 'WAITING' || status === 'QUEUED' ? 'PENDING' : job.run.status })) })
  const requests = active ? [request(run.runId || workflow.WORKFLOW_ID, run.status)] : []
  for (let index = run.status === 'WAITING' ? 1 : 0; index < waitingCount; index++) requests.push(request(`waiting-${workflow.WORKFLOW_ID}-${index}`, 'WAITING', false))
  return { ...workflow, execution: actual, lastResult, planning, jobs, requests: workflow.requests || requests }
}

async function setup(page, { started = false, healthUnavailable = false, waitForMonitor = true, hidden = false, primary = false } = {}) {
  await page.clock.install({ time: new Date(stamp) })
  await page.addInitScript(hidden => {
    window.__testVisibility = hidden ? 'hidden' : 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => window.__testVisibility })
    window.__eventUrls = []
    window.__eventStreams = []
    window.EventSource = class {
      constructor(url) { this.url = url; this.closed = false; window.__eventUrls.push(url); window.__eventStreams.push(this) }
      addEventListener() {}
      close() { this.closed = true }
    }
  }, hidden)
  const orphan = row('unassigned', 'Unassigned jobs')
  orphan.isUnassigned = true; orphan.IS_ENABLED = false; orphan.canRun = false; orphan.jobs[0].JOB_NAME = 'Unassigned SQL'
  orphan.jobs[0].runOnly = true
  const state = { data: { ok: true, source: 'POSTGRES', rows: [row(wf, 'WF1'), row(child, 'WF2'), row(dbt, 'DBT workflow', 'DBT'), orphan],
    workflowsCount: 3, jobsCount: 4, unassignedJobs: 1, runsEnabled: true, refreshSeconds: 2, generatedAt: stamp, configurationRevision: 1,
    submissions: [], warnings: [], dispatchErrors: [], worker: { state: 'HEALTHY' }, scheduleMessage: 'Imported schedules are not enabled in the PostgreSQL engine' },
    requests: [], writes: [], errors: [], fail: false, rejectStart: false, conflict: false, healthUnavailable }
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
    if (path === '/api/health') {
      if (state.healthUnavailable) return route.fulfill({ status: 503, json: { error: 'Backend temporarily unavailable' } })
      body = { ok: true, environment: 'DEV', taskPrototypeEnabled: true, legacyEngineRetired: true, postgresEnginePrimary: primary }
    }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === prefix + '/monitor') {
      if (state.fail) return route.fulfill({ status: 503, json: { error: 'PostgreSQL status unavailable' } })
      body = { ...state.data, rows: state.data.rows.map(monitorRow) }
    }
    if (path === prefix + '/graphs/graph/plan') {
      if (state.rejectPreview) return route.fulfill({ status: 503, json: { error: 'Plan temporarily unavailable' } })
      const params = new URL(req.url()).searchParams
      const workflowId = params.get('workflowId'), includeChildren = params.get('includeChildren') === 'true'
      const nodes = state.data.rows.filter(row => row.WORKFLOW_ID === workflowId || (includeChildren && workflowId === wf && row.WORKFLOW_ID === child))
        .map(row => ({ key: row.WORKFLOW_ID, workflowId: row.WORKFLOW_ID, name: row.WORKFLOW_NAME, mode: 'SEQUENTIAL',
          parents: row.WORKFLOW_ID === child && workflowId !== child ? [{ key: wf, name: 'WF1', inPlan: true, allow: true, success: true, warning: false, error: false, parentMustBeNewer: true }] : [],
          jobs: row.jobs.map(job => ({ key: job.JOB_ID, name: job.JOB_NAME, type: job.JOB_TYPE, layer: job.JOB_LAYER })), issues: [] }))
      body = state.plan || { ok: true, rootKey: workflowId, includeChildren, configurationRevision: 1, nodes, omitted: [], issues: [] }
      if (state.previewGate && includeChildren) await state.previewGate
    }
    if (req.method() !== 'GET') state.writes.push({ path, body: req.postData() ? req.postDataJSON() : null })
    if (path === prefix + '/graphs/graph/runs') {
      if (state.rejectStart) return route.fulfill({ status: 503, json: { error: 'Admission response unavailable' } })
      const root = state.data.rows.find(w => w.WORKFLOW_ID === req.postDataJSON().workflowId), waiting = state.data.rows[1]
      root.run = { status: 'QUEUED', runId: execution, requestedAt: stamp, message: '' }; root.canRun = false; root.canCancel = true; root.cascadeId = 'cascade'
      if (req.postDataJSON().includeChildren && root.WORKFLOW_ID === wf) { waiting.run.status = 'WAITING'; waiting.run.message = 'WF1: Waiting for parent completion' }
      body = { ok: true, cascadeId: 'cascade' }
    }
    if (req.method() !== 'GET' && (path === prefix + '/schedules' || path === prefix + '/schedules/schedule')) {
      const settings = req.postDataJSON(), root = state.data.rows[0]
      root.IS_SCHEDULE_ENABLED = settings.enabled; root.SCHEDULE = settings.cron; root.SCHEDULE_TIMEZONE = settings.timezone
      root.scheduler = { id: 'schedule', revision: (root.scheduler?.revision || 0) + 1, state: settings.enabled ? 'STARTED' : 'SUSPENDED', nextRun: settings.enabled ? '2026-11-23T14:01:00Z' : null }
    }
    if (path === prefix + '/cascades/cascade/cancel') {
      state.data.rows[0].cancelRequested = true; state.data.rows[0].run.message = 'Cancellation requested; awaiting task confirmation'
    }
    if (req.method() === 'POST' && path.endsWith('/recover')) {
      if (state.rejectRecovery) return route.fulfill({ status: 503, json: { error: 'Task recovery unavailable' } })
      if (state.recoveryGate) await state.recoveryGate
      const recovering = state.data.rows.find(w => w.run.recovery?.runId === path.split('/').at(-2))
      recovering.run = { ...recovering.run, status: 'QUEUED', message: '', recovery: null }
      body = { ok: true, recoveredAttempts: 1 }
    }
    if (req.method() === 'POST' && path.endsWith('/retry')) {
      if (state.rejectWorkflowRetry) return route.fulfill({ status: 409, json: { error: 'A newer execution already exists; refresh the Monitor' } })
      if (state.workflowRetryGate) await state.workflowRetryGate
      const failed = state.data.rows.find(w => w.run.retry?.nodeId === path.split('/').at(-2))
      failed.run = { ...failed.run, status: 'QUEUED', message: '', retry: null }; failed.canRun = false
      for (const blocked of state.data.rows.filter(w => w.run.status === 'BLOCKED')) {
        blocked.run = { ...blocked.run, status: 'WAITING', message: 'Waiting for parent WF2 (queued) to complete' }
        blocked.waitingCount = 1; blocked.canRun = false
      }
      body = { ok: true, cascadeId: 'recovery-group' }
    }
    if (req.method() === 'POST' && path.endsWith('/retry-dispatch')) {
      if (state.rejectRetry) return route.fulfill({ status: 503, json: { error: 'Dependency retry unavailable' } })
      if (state.retryGate) await state.retryGate
      const cascadeId = path.split('/').at(-2)
      state.data.dispatchErrors = state.data.dispatchErrors.filter(error => error.cascadeId !== cascadeId)
      for (const workflow of state.data.rows) {
        if (workflow.run.dispatch?.cascadeId !== cascadeId) continue
        workflow.dispatch = workflow.run.dispatch = { ...workflow.run.dispatch, state: 'PENDING', attempts: 0, error: null, canRetry: false }
        if (workflow.run.status === 'WAITING') workflow.run.message = 'Waiting for parent completion'
      }
      body = { ok: true, cascadeId }
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
  if (waitForMonitor) {
    await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
    await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeVisible()
  }
  return state
}

function pgOnly(state) {
  expect(state.requests.filter(path => path.startsWith('/api/orchestration/') && !path.startsWith(prefix))).toEqual([])
  expect(state.errors).toEqual([])
}

test('primary SPCS engine overrides a saved Snowflake choice and keeps presence on PostgreSQL', async ({ page }) => {
  await page.addInitScript(() => window.sessionStorage.setItem('kumoConfigurationSource','SNOWFLAKE'))
  const state=await setup(page,{primary:true})
  await expect(page.getByLabel('Configuration source',{exact:true})).toBeDisabled()
  await expect(page.getByLabel('Configuration source',{exact:true})).toHaveValue('POSTGRES')
  expect((await page.evaluate(() => window.__eventUrls)).every(url => url.includes('orchestrationSource=POSTGRES'))).toBe(true)
  expect(state.requests).not.toContain('/api/snowflake/ping')
  pgOnly(state)
})

test('running PostgreSQL DBT jobs show native task execution without promising model polling', async ({ page }) => {
  const state=await setup(page)
  const workflow=state.data.rows[2]
  workflow.run={status:'RUNNING',runId:'dbt-node',start:stamp,message:'',source:'POSTGRES'}
  workflow.jobs[0].run={status:'RUNNING',runId:'dbt-node',start:stamp,source:'POSTGRES'}
  await page.getByRole('button',{name:'Refresh',exact:false}).first().click()
  await page.getByRole('button',{name:'Show jobs for DBT workflow',exact:true}).click()
  await expect(page.getByRole('table')).toContainText('DBT task running')
  await expect(page.getByRole('table')).not.toContainText('Waiting for dbt progress')
  pgOnly(state)
})

test('enabled PostgreSQL DBT workflows use the shared Run preview and show native completion', async ({ page }) => {
  const state = await setup(page)
  const workflow = state.data.rows[2]
  workflow.canRun = true; workflow.canRunWithChildren = true; workflow.runUnavailable = ''
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await page.getByRole('button', { name: 'Run workflow DBT workflow', exact: true }).click()
  await page.getByLabel('Execution scope', { exact: true }).selectOption('WORKFLOW')
  const plan = page.getByRole('region', { name: 'Execution plan', exact: true })
  await expect(plan).toContainText('1 workflow')
  await plan.locator('.run-plan-entry > summary').click()
  await plan.getByText('1 job · Sequential', { exact: true }).click()
  await expect(plan).toContainText('DBT · GENERAL')
  await page.getByRole('dialog').getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.writes).toHaveLength(1)
  expect(state.writes[0]).toMatchObject({ path: prefix + '/graphs/graph/runs', body: { workflowId: dbt, includeChildren: false } })
  workflow.run = { status: 'RUNNING', runId: 'dbt-native', start: stamp, source: 'POSTGRES' }
  workflow.jobs[0].run = { ...workflow.run, jobId: dbt, type: 'DBT' }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(page.getByRole('button', { name: 'Hide jobs for DBT workflow', exact: true })).toBeVisible()
  await expect(page.getByRole('table')).toContainText('DBT task running')
  workflow.run = { ...workflow.run, status: 'SUCCESS', end: '2026-11-23T14:01:00Z' }
  workflow.jobs[0].run = { ...workflow.jobs[0].run, status: 'SUCCESS', end: workflow.run.end }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for DBT workflow', exact: true }) })
  await expect(display.locator('.om-execution-status')).toContainText('SUCCESS')
  await expect(display.locator('.om-planning-cell')).toBeEmpty()
  pgOnly(state)
})

test('Last result keeps the previous outcome while State follows running, review and completion', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0], old = structuredClone(workflow.run)
  const activeJob = { ...old.jobs[0], status: 'RUNNING', runId: 'new-run', start: stamp, end: null }
  workflow.run = { ...old, status: 'RUNNING', runId: 'new-run', start: stamp, end: null, jobs: [activeJob] }
  workflow.jobs[0].run = activeJob; workflow.canRun = false; workflow.waitingCount = 2
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  await expect(page.getByRole('columnheader', { name: 'Last run', exact: true })).toBeVisible()
  await expect(display.locator('.om-execution-status')).toHaveText(/SUCCESS/)
  await expect(display.locator('.om-execution-status')).toHaveAttribute('title', /Last completed execution/)
  await expect(display.locator('.om-planning-cell')).toContainText('STARTED')
  await expect(display.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(display.locator('.om-execution-status .om-spinner')).toHaveCount(0)
  await expect(display.locator('.om-planning-cell .om-spinner')).toHaveCount(1)
  await page.getByRole('button', { name: 'Show jobs for WF1', exact: true }).click()
  const jobRow = page.locator('#om-jobs-' + wf).getByRole('row').first()
  await expect(jobRow.locator('.om-execution-status')).toHaveText(/SUCCESS/)
  await expect(jobRow.locator('.om-planning-cell')).toHaveText(/STARTED/)
  await expect(page.getByLabel('Filter workflow result', { exact: true }).locator('option[value="RUNNING"]')).toHaveCount(0)
  await page.getByLabel('Filter workflow result', { exact: true }).selectOption('SUCCESS')
  await page.getByLabel('Filter workflow activity', { exact: true }).selectOption('STARTED')
  await expect(display).toBeVisible()
  await page.getByLabel('Filter workflow result', { exact: true }).selectOption('')
  await page.getByLabel('Filter workflow activity', { exact: true }).selectOption('')
  workflow.run.status = 'UNKNOWN'; activeJob.status = 'UNKNOWN'; workflow.waitingCount = 0
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(display.locator('.om-execution-status')).toHaveText(/SUCCESS/)
  await expect(display.locator('.om-planning-cell')).toContainText('NEEDS REVIEW')
  await expect(jobRow.locator('.om-planning-cell')).toHaveText(/NEEDS REVIEW/)
  workflow.run = { ...workflow.run, status: 'FAILED', end: '2026-11-23T14:01:00Z', jobs: [{ ...activeJob, status: 'FAILED', end: '2026-11-23T14:01:00Z' }] }
  workflow.jobs[0].run = workflow.run.jobs[0]; workflow.canRun = true
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(display.locator('.om-execution-status')).toHaveText(/FAILED/)
  await expect(display.locator('.om-planning-cell')).toBeEmpty()
  await expect(jobRow.locator('.om-execution-status')).toHaveText(/FAILED/)
  expect(state.writes).toEqual([])
  pgOnly(state)
})

function recoverableWorkflow(state) {
  const workflow = row('wf4', 'WF4')
  workflow.run = { status: 'UNKNOWN', runId: 'wf4-node', start: null,
    message: 'Task recovery paused: OperationalError: task creation unavailable',
    recovery: { automatic: false, canRecover: true, runId: 'native-wf4', nextRetryAt: null, attempts: 6 } }
  workflow.canRun = false
  state.data.rows.push(workflow)
  return workflow
}

test('temporary Snowflake connection failure shows automatic recovery and next retry', async ({ page }) => {
  const state = await setup(page)
  const workflow = recoverableWorkflow(state)
  workflow.run = { ...workflow.run, status: 'QUEUED',
    message: 'Snowflake connection unavailable: DNS_LOOKUP_FAILED; automatic recovery scheduled',
    recovery: { ...workflow.run.recovery, automatic: true, canRecover: false, nextRetryAt: '2026-11-23T14:01:00Z', attempts: 8 } }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF4', exact: true }) })
  await expect(display).toContainText('QUEUED')
  await expect(display).toContainText('DNS_LOOKUP_FAILED')
  await expect(display).toContainText('automatic recovery scheduled')
  await expect(display).toContainText('Next connection retry')
  await expect(page.getByRole('button', { name: 'Recover workflow WF4', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run workflow WF4', exact: true })).toBeDisabled()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('recover an UNKNOWN native attempt once and refresh its existing workflow', async ({ page }) => {
  const state = await setup(page)
  recoverableWorkflow(state)
  let release
  state.recoveryGate = new Promise(resolve => { release = resolve })
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const button = page.getByRole('button', { name: 'Recover workflow WF4', exact: true })
  await button.click()
  await expect(button).toBeDisabled()
  await button.evaluate(element => element.click())
  release()
  await expect(button).toHaveCount(0)
  await expect(page.getByText('WF4 recovery queued.', { exact: false })).toBeVisible()
  expect(state.writes).toEqual([{ path: prefix + '/runs/native-wf4/recover', body: null }])
  pgOnly(state)
})

test('failed native recovery keeps the UNKNOWN state and allows retry', async ({ page }) => {
  const state = await setup(page)
  recoverableWorkflow(state); state.rejectRecovery = true
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const button = page.getByRole('button', { name: 'Recover workflow WF4', exact: true })
  await button.click()
  await expect(page.getByText('Task recovery unavailable', { exact: true })).toBeVisible()
  await expect(button).toBeEnabled()
  await expect(page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF4', exact: true }) })).toContainText('NEEDS REVIEW')
  pgOnly(state)
})

test('disabled execution controls prevent native recovery requests', async ({ page }) => {
  const state = await setup(page)
  recoverableWorkflow(state); state.data.runsEnabled = false
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(page.getByRole('button', { name: 'Recover workflow WF4', exact: true })).toBeDisabled()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

function failedBranch(state) {
  const failed = state.data.rows[1]
  failed.run = { status: 'FAILED', runId: 'failed-wf2', start: stamp,
    retry: { cascadeId: 'failed-group', nodeId: 'failed-node' }, message: '' }
  const sibling = row('wf3', 'WF3')
  sibling.run = { status: 'SUCCESS', runId: 'successful-wf3', start: stamp, end: stamp, message: '' }
  const blocked = row('wf4', 'WF4')
  blocked.run = { status: 'BLOCKED', runId: 'blocked-wf4', message: 'Parent WF2 (failed): Parent outcome does not permit execution' }
  state.data.rows.push(sibling, blocked)
  return { failed, sibling, blocked }
}

test('retry a failed branch once, restore WAITING descendants and retain WF3 success', async ({ page }) => {
  const state = await setup(page)
  const { sibling } = failedBranch(state)
  let release
  state.workflowRetryGate = new Promise(resolve => { release = resolve })
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const button = page.getByRole('button', { name: 'Retry failed workflow WF2', exact: true })
  await expect(button).toHaveAttribute('title', /Completed workflows are retained/)
  await button.click()
  await expect(button).toBeDisabled()
  await button.evaluate(element => element.click())
  release()
  await expect(button).toHaveCount(0)
  const workflow = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF4', exact: true }) })
  await expect(workflow).toContainText('PLANNED')
  await expect(workflow.locator('.om-request').first()).toHaveAttribute('title', /Waiting for parent WF2/)
  await expect(page.getByText('WF2 retry accepted.', { exact: false })).toBeVisible()
  expect(sibling.run).toMatchObject({ status: 'SUCCESS', runId: 'successful-wf3' })
  expect(state.writes).toEqual([{ path: prefix + '/cascades/failed-group/nodes/failed-node/retry', body: null }])
  pgOnly(state)
})

test('a rejected workflow retry preserves FAILED and BLOCKED states', async ({ page }) => {
  const state = await setup(page)
  failedBranch(state); state.rejectWorkflowRetry = true
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const button = page.getByRole('button', { name: 'Retry failed workflow WF2', exact: true })
  await button.click()
  await expect(page.getByText('A newer execution already exists; refresh the Monitor', { exact: true })).toBeVisible()
  await expect(button).toBeEnabled()
  expect(state.data.rows[1].run.status).toBe('FAILED')
  expect(state.data.rows.find(w => w.WORKFLOW_NAME === 'WF4').run.status).toBe('BLOCKED')
  pgOnly(state)
})

test('execution controls and direct neighbor guards disable failed workflow retry', async ({ page }) => {
  const state = await setup(page)
  const { failed } = failedBranch(state)
  failed.canRun = false; failed.runUnavailable = 'Waiting for parent WF1 (running) to complete'
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const button = page.getByRole('button', { name: 'Retry failed workflow WF2', exact: true })
  await expect(button).toBeDisabled()
  await expect(button).toHaveAttribute('title', /parent WF1/)
  failed.canRun = true; state.data.runsEnabled = false
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(button).toBeDisabled()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

function pauseDispatch(state) {
  const failure = { cascadeId: 'paused-group', workflowNames: ['WF1', 'WF2'], requestedAt: stamp,
    attempts: 6, error: 'Dependency evaluation unavailable: RuntimeError', canRetry: true }
  state.data.dispatchErrors = [failure]
  for (const workflow of state.data.rows.slice(0, 2)) {
    workflow.dispatch = workflow.run.dispatch = { ...failure, state: 'ERROR' }
    workflow.canRun = false
    if (workflow.run.status === 'SUCCESS') continue
    workflow.run = { ...workflow.run, status: 'WAITING', runId: execution, cascadeId: failure.cascadeId,
      message: 'Dependency dispatch paused after 6 attempts. ' + failure.error }
  }
}

test('dispatch errors remain visible across filters and retry retains successful outcomes', async ({ page }) => {
  const state = await setup(page, { started: true })
  pauseDispatch(state)
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const alert = page.locator('.om-dispatch-error')
  await expect(alert).toContainText('Dependency dispatch paused')
  await expect(alert).toContainText('6 attempts')
  await expect(alert).toContainText('RuntimeError')
  await expect(page.locator('.om-engine-status')).toContainText('Execution worker: HEALTHY')
  await expect(page.locator('.om-engine-status')).toContainText('Dependency dispatch: 1 paused group')
  await expect(page.getByRole('table')).toContainText('SUCCESS')
  await expect(page.getByRole('table')).toContainText('Group dispatch paused')
  await page.getByLabel('Filter workflow result', { exact: true }).selectOption('SUCCESS')
  await expect(page.getByRole('button', { name: 'Run workflow WF2', exact: true })).toHaveCount(0)
  await expect(alert).toBeVisible()
  await page.getByRole('button', { name: 'Timeline', exact: true }).click()
  await expect(alert).toBeVisible()
  await page.getByRole('button', { name: 'Table', exact: true }).click()
  await alert.getByRole('button', { name: 'Retry dependency dispatch for WF1, WF2', exact: true }).click()
  await expect(alert).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: 'Dependency dispatch queued for retry' })).toContainText('Completed and running jobs are retained')
  await expect(page.getByRole('table')).toContainText('SUCCESS')
  expect(state.writes).toEqual([{ path: prefix + '/cascades/paused-group/retry-dispatch', body: null }])
  expect(state.data.rows[0].run.status).toBe('SUCCESS')
  pgOnly(state)
})

test('failed dispatch retry keeps the group alert and permits another attempt', async ({ page }) => {
  const state = await setup(page)
  pauseDispatch(state); state.rejectRetry = true
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await page.getByRole('button', { name: 'Retry dependency dispatch for WF1, WF2', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Dependency retry unavailable' })).toBeVisible()
  await expect(page.locator('.om-dispatch-error')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry dependency dispatch for WF1, WF2', exact: true })).toBeEnabled()
  expect(state.writes).toHaveLength(1)
  pgOnly(state)
})

test('dispatch retry is disabled when execution controls are disabled', async ({ page }) => {
  const state = await setup(page)
  pauseDispatch(state); state.data.runsEnabled = false
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(page.locator('.om-dispatch-error')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry dependency dispatch for WF1, WF2', exact: true })).toBeDisabled()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('dispatch retry prevents duplicate submissions while its request is pending', async ({ page }) => {
  const state = await setup(page)
  pauseDispatch(state)
  let release
  state.retryGate = new Promise(resolve => { release = resolve })
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const retry = page.getByRole('button', { name: 'Retry dependency dispatch for WF1, WF2', exact: true })
  await retry.click()
  await expect(retry).toBeDisabled()
  await retry.click({ force: true })
  expect(state.writes).toHaveLength(1)
  release()
  await expect(page.locator('.om-dispatch-error')).toHaveCount(0)
  pgOnly(state)
})

test('existing Monitor shows every catalog job and uses PostgreSQL presence without Snowflake readers', async ({ page }) => {
  const state = await setup(page)
  await expect(page.locator('.om-notice')).toContainText('3 workflows · 4 jobs · 1 unassigned')
  await page.getByRole('button', { name: 'Expand all', exact: true }).click()
  await expect(page.getByRole('table')).toContainText('Unassigned SQL')
  await expect(page.getByRole('button', { name: 'Run workflow DBT workflow', exact: true })).toHaveAttribute('title', /DBT execution is not enabled/)
  await expect(page.getByRole('button', { name: 'Run workflow DBT workflow', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Run WF1 job in WF1', exact: true })).toBeDisabled()
  await expect(page.getByRole('region', { name: 'Workflow dependency trial' })).toHaveCount(0)
  expect((await page.evaluate(() => window.__eventUrls)).every(url => url.includes('orchestrationSource=POSTGRES'))).toBe(true)
  pgOnly(state)
})

test('a background tab loads configuration without occupying a realtime connection', async ({ page }) => {
  const state = await setup(page, { hidden: true })
  expect(await page.evaluate(() => window.__eventStreams.length)).toBe(0)
  await page.evaluate(() => { window.__testVisibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')) })
  await expect.poll(() => page.evaluate(() => window.__eventStreams.filter(stream => !stream.closed).length)).toBe(1)
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(await page.evaluate(() => window.__eventStreams.length)).toBe(1)
  pgOnly(state)
})

test('hiding a tab closes its realtime stream and returning reconnects once', async ({ page }) => {
  const state = await setup(page)
  await expect.poll(() => page.evaluate(() => window.__eventStreams.filter(stream => !stream.closed).length)).toBe(1)
  await page.evaluate(() => { window.__testVisibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
  expect(await page.evaluate(() => window.__eventStreams.filter(stream => !stream.closed).length)).toBe(0)
  await page.evaluate(() => { window.__testVisibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')) })
  await expect.poll(() => page.evaluate(() => window.__eventStreams.filter(stream => !stream.closed).length)).toBe(1)
  expect(await page.evaluate(() => window.__eventStreams.length)).toBe(2)
  expect((await page.evaluate(() => window.__eventUrls)).every(url => url.includes('orchestrationSource=POSTGRES'))).toBe(true)
  pgOnly(state)
})

test('root Run starts the full dependency group once and shows queued and waiting states', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Start here and evaluate downstream workflows')
  const plan = page.getByRole('region', { name: 'Execution plan', exact: true })
  await expect(plan).toContainText('2 workflows')
  await expect(plan.locator('.run-plan-node')).toHaveCount(2)
  await plan.locator('.run-plan-entry > summary').click()
  await plan.getByText('1 job · Sequential', { exact: true }).first().click()
  await expect(plan.getByText('WF1 job', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.writes[0]).toMatchObject({ path: prefix + '/graphs/graph/runs', body: { workflowId: wf } })
  expect(Object.keys(state.writes[0].body).sort()).toEqual(['includeChildren', 'requestId', 'workflowId'])
  expect(state.writes[0].body.includeChildren).toBe(true)
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeDisabled()
  await expect(page.getByRole('table')).toContainText('PLANNED')
  pgOnly(state)
})

test('a middle workflow can be run alone without starting its dependencies', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Run workflow WF2', exact: true }).click()
  await page.getByLabel('Execution scope', { exact: true }).selectOption('WORKFLOW')
  await expect(page.getByRole('region', { name: 'Execution plan', exact: true })).toContainText('1 workflow')
  await expect(page.getByRole('dialog')).toContainText('This run does not start downstream workflows')
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.writes[0]).toMatchObject({ path: prefix + '/graphs/graph/runs', body: { workflowId: child, includeChildren: false } })
  expect(state.data.rows[0].run.status).toBe('NOT RUN')
  pgOnly(state)
})

test('compact plan shows joins once, describes outcomes, and keeps details collapsed', async ({ page }) => {
  const state = await setup(page)
  const parent = (key, name, inPlan = true) => ({ key, name, inPlan, allow: true, success: true, warning: false, error: false, parentMustBeNewer: true })
  const node = (key, name, parents = []) => ({ key, name, parents, mode: 'SEQUENTIAL', jobs: [], issues: [] })
  state.plan = { rootKey: wf, nodes: [node(wf, 'WF1'), node('left', 'WF2', [parent(wf, 'WF1')]), node('right', 'WF3', [parent(wf, 'WF1')]),
    node('join', 'WF4', [parent('left', 'WF2'), { ...parent('right', 'WF3'), warning: null }, parent('external', 'WF5', false)]),
    node('failure', 'WF7', [{ ...parent('join', 'WF4'), success: false, error: true }])], omitted: [{ key: 'disabled', name: 'WF6' }], issues: [] }
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  const plan = page.getByRole('region', { name: 'Execution plan', exact: true })
  await expect(plan.locator('.run-plan-node')).toHaveCount(5)
  await expect(plan.locator('.run-plan-node[open]')).toHaveCount(0)
  await expect(plan.locator('.run-plan-heading > strong')).toHaveText(['WF1', 'WF2', 'WF3', 'WF4', 'WF7'])
  const join = plan.locator('.run-plan-heading').filter({ has: page.getByText('WF4', { exact: true }) })
  await expect(join).toContainText('WF2: On parent Success')
  await expect(join).toContainText('WF3: On parent Success, ignore warnings')
  await expect(join).toContainText('WF5 (existing result)')
  await expect(plan.locator('.run-plan-heading').filter({ has: page.getByText('WF7', { exact: true }) })).toContainText('On parent Fail')
  await join.click()
  await expect(plan.getByRole('list', { name: 'Parents of WF4' })).toContainText('WF2')
  await expect(plan.getByRole('list', { name: 'Parents of WF4' })).toContainText('WF3')
  await expect(plan.getByRole('list', { name: 'Parents of WF4' })).toContainText('WF5Existing result · not rerun')
  await expect(plan).toContainText('Disabled workflows omitted: WF6')
  await expect(plan).toContainText('parent must finish after this workflow’s previous start')
  expect(state.writes).toEqual([])
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('failed previews prevent submission and can be retried without starting work', async ({ page }) => {
  const state = await setup(page)
  state.rejectPreview = true
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Plan temporarily unavailable')
  await expect(page.getByRole('button', { name: 'Run workflow', exact: true })).toBeDisabled()
  state.rejectPreview = false
  await page.getByRole('button', { name: 'Retry preview', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Execution plan', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run workflow', exact: true })).toBeEnabled()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('changing scope discards a late response for the previous plan', async ({ page }) => {
  const state = await setup(page)
  let release
  state.previewGate = new Promise(resolve => { release = resolve })
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Loading execution plan')
  await expect(page.getByRole('button', { name: 'Run workflow', exact: true })).toBeDisabled()
  await page.getByLabel('Execution scope', { exact: true }).selectOption('WORKFLOW')
  const plan = page.getByRole('region', { name: 'Execution plan', exact: true })
  await expect(plan.locator('.run-plan-node')).toHaveCount(1)
  release()
  await expect(plan).toContainText('1 workflow')
  await expect(plan.locator('.run-plan-node')).toHaveCount(1)
  expect(state.writes).toEqual([])
})

test('related active work disables Run buttons with the blocker shown in the GUI', async ({ page }) => {
  const state = await setup(page)
  for (const workflow of state.data.rows.slice(0, 2)) {
    workflow.canRun = false; workflow.runUnavailable = 'Waiting for WF3 (queued, running or needs review)'
  }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  for (const name of ['WF1', 'WF2']) {
    const button = page.getByRole('button', { name: `Run workflow ${name}`, exact: true })
    await expect(button).toBeDisabled()
    await expect(button).toHaveAttribute('title', /WF3/)
  }
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('running WF10 allows WF1 and WF2 while adjacent WF8 and WF9 show waiting', async ({ page }) => {
  const state = await setup(page)
  const leaf = row('44444444-4444-4444-8444-444444444444', 'WF10')
  leaf.run = { status: 'RUNNING', runId: 'leaf-run', start: stamp, message: '' }; leaf.canRun = false; leaf.waitingCount = 2
  for (const name of ['WF8','WF9']) {
    const adjacent = row(`88888888-8888-4888-8888-${name === 'WF8' ? '888888888888' : '999999999999'}`, name)
    adjacent.run = { status: 'WAITING', runId: `waiting-${name}`, start: null, message: 'WF10: Waiting for related execution to complete',
      waitingFor: [{ name: 'WF10', status: 'RUNNING', relationship: 'CHILD' }] }
    adjacent.canRun = false;adjacent.runUnavailable = 'Workflow is waiting for dependencies';adjacent.waitingCount = 1
    state.data.rows.push(adjacent)
  }
  state.data.rows.push(leaf);state.data.workflowsCount += 3
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(page.getByRole('button', { name: 'Run workflow WF10', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Run workflow WF8', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Run workflow WF9', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Run workflow WF2', exact: true })).toBeEnabled()
  const running = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF10', exact: true }) })
  await expect(running).toContainText('STARTED')
  await expect(running.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(running.locator('.om-request')).toHaveCount(3)
  await expect(running.locator('.om-request-meta')).toContainText(['#1', '#2', '#3'])
  await expect(running.locator('.om-name-detail')).not.toContainText('2 waiting')
  await expect(running.locator('.om-name-detail')).not.toContainText('queued')
  for (const name of ['WF8','WF9']) {
    const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: `Execution history for ${name}`, exact: true }) })
    await expect(display.locator('.om-planning-cell')).toContainText('PLANNED')
    await expect(display).not.toContainText('QUEUED')
    await expect(display.getByRole('img', { name: 'Preparing execution' })).toHaveCount(0)
  }
  await expect(page.locator('.om-planning-cell .om-dependency-item').filter({ hasText: 'Child WF10' })).toHaveCount(0)
  leaf.waitingCount = 1
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(running.locator('.om-planning-cell')).toContainText('PLANNED')
  leaf.waitingCount = 0
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(running.locator('.om-planning-cell')).not.toContainText('PLANNED')
  await expect(running.locator('.om-live-status')).toContainText('NO RESULT')
  await expect(running.locator('.om-planning-cell')).toContainText('STARTED')
  await expect(page.getByLabel('Filter workflow activity', { exact: true }).locator('option[value="EXECUTING"]')).toHaveCount(0)
  leaf.IS_SCHEDULE_ENABLED = true
  leaf.scheduler = { state: 'STARTED', nextRun: '2026-11-23T14:01:00Z' }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(running.locator('.om-live-status')).toContainText('NO RESULT')
  await expect(running.locator('.om-planning-cell')).toContainText('STARTED')
  await expect(running.locator('.om-planning-cell')).not.toContainText('EXECUTING')
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Run workflow WF1', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('its direct parents and children must be idle')
  await expect(page.getByRole('button', { name: 'Run workflow', exact: true })).toBeEnabled()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('WF4 State shows only direct parents while child waits still disable Run', async ({ page }) => {
  const state = await setup(page)
  const waiting = row('wf4', 'WF4')
  waiting.run = { status: 'WAITING', runId: 'wf4-waiting', requestedAt: stamp, start: null,
    message: 'Waiting for parent WF2 (running) to complete; Waiting for parent WF3 (running) to complete; Waiting for child WF5 (running) to complete',
    waitingFor: [{ name: 'WF2', status: 'RUNNING', relationship: 'PARENT' }, { name: 'WF3', status: 'RUNNING', relationship: 'PARENT' }, { name: 'WF5', status: 'RUNNING', relationship: 'CHILD' }] }
  waiting.canRun = false; waiting.runUnavailable = 'Workflow is waiting for dependencies'; waiting.waitingCount = 2
  state.data.rows.push(waiting)
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const workflow = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF4', exact: true }) })
  const detail = workflow.locator('.om-request').first()
  const button = page.getByRole('button', { name: 'Run workflow WF4', exact: true })
  await expect(workflow.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(detail.locator('.om-dependency-item')).toHaveCount(2)
  await expect(detail).toContainText('Parent WF2RUNNING')
  await expect(detail).toContainText('Parent WF3RUNNING')
  await expect(detail).not.toContainText('WF5')
  await expect(detail).toContainText('Waiting for')
  await expect(detail).not.toContainText('to complete')
  await expect(button).toBeDisabled()
  await expect(button).toHaveAttribute('title', /parent WF2.*parent WF3.*child WF5/)
  await expect(detail).not.toHaveClass(/om-error/)
  expect((await detail.textContent()).match(/WF2/g)).toHaveLength(1)
  waiting.run.message = 'Waiting for child WF5 (running) to complete'
  waiting.run.waitingFor = waiting.run.waitingFor.slice(2)
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(detail.locator('.om-dependency-item')).toHaveCount(0)
  await expect(detail).not.toContainText('WF5')
  await expect(button).toBeDisabled()
  await expect(button).toHaveAttribute('title', /child WF5/)
  await expect(workflow.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(detail).not.toContainText('WF2')
  await expect(detail).not.toContainText('WF3')
  waiting.run.message = 'Waiting for child WF5 (needs review) to complete'
  waiting.run.waitingFor[0].status = 'UNKNOWN'
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(workflow).toContainText('PLANNED')
  await expect(detail).not.toContainText('WF5')
  await expect(detail).not.toContainText('UNKNOWN')
  await expect(button).toBeDisabled()
  await expect(detail).toHaveAttribute('title', /needs review/)
  await expect(detail).not.toHaveClass(/om-error/)
  waiting.run = { ...waiting.run, status: 'QUEUED', message: '', waitingFor: [] }
  waiting.waitingCount = 0; waiting.queuedCount = 1
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(workflow).toContainText('QUEUED')
  await expect(workflow).not.toContainText('PLANNED')
  await expect(detail).toContainText('QUEUED')
  await expect(detail).not.toContainText('1 waiting')
  await expect(workflow.getByRole('img', { name: 'Preparing execution' })).toBeVisible()
  waiting.run = { ...waiting.run, status: 'RUNNING', start: stamp, message: '', waitingFor: [] }
  waiting.queuedCount = 0
  waiting.runUnavailable = ''
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(workflow).toContainText('STARTED')
  await expect(detail).not.toContainText('Waiting')
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('a missing parent outcome is BLOCKED and explains why the join cannot execute', async ({ page }) => {
  const state = await setup(page)
  const blocked = row('wf7', 'WF7')
  blocked.run = { status: 'BLOCKED', runId: 'wf7-blocked', start: null,
    message: 'Parent WF6: No parent execution was available when this run was requested',
    waitingFor: [{ name: 'WF6', status: null, relationship: 'PARENT' }] }
  state.data.rows.push(blocked)
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const workflow = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF7', exact: true }) })
  await expect(workflow).toContainText('BLOCKED')
  await expect(workflow.locator('.om-request').first()).toHaveClass(/om-error/)
  await expect(workflow).toContainText('Parent WF6MISSING')
  await expect(workflow.locator('.om-request').first()).toHaveAttribute('title', /No parent execution was available/)
  await expect(workflow).not.toContainText('WF6 (null)')
  await expect(workflow).not.toContainText('Waiting to start')
  pgOnly(state)
})

test('compact waits deduplicate direct dependencies and keep execution count independent', async ({ page }) => {
  const state = await setup(page)
  const waiting = row('wf4', 'WF4')
  waiting.waitingCount = 4; waiting.canRun = false
  waiting.run = { status: 'WAITING', runId: 'wf4-waiting', message: 'Full saved dependency explanation',
    waitingFor: [
      { name: 'WF2', nodeKey: 'wf2', relationship: 'PARENT', status: 'RUNNING' },
      { name: 'WF2', nodeKey: 'wf2', relationship: 'PARENT', status: 'RUNNING' },
      { name: 'WF3', nodeKey: 'wf3', relationship: 'PARENT', status: 'ADMITTED' },
      { name: 'WF1', nodeKey: 'wf1', relationship: 'GRANDPARENT', status: 'RUNNING' },
      { name: 'WF5', nodeKey: 'wf5', relationship: 'CHILD', status: 'UNKNOWN' },
      { name: 'WF4', nodeKey: 'wf4', relationship: 'SELF', status: 'RUNNING' }
    ] }
  waiting.jobs[0].run = { status: 'PENDING', message: 'Full saved dependency explanation' }
  state.data.rows.push(waiting)
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const workflow = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF4', exact: true }) })
  const detail = workflow.locator('.om-request').first()
  await expect(workflow.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(detail.locator('.om-dependency-item')).toHaveCount(2)
  await expect(detail).toContainText('Parent WF2RUNNING')
  await expect(detail).toContainText('Parent WF3QUEUED')
  await expect(detail).not.toContainText('WF1')
  await expect(detail).not.toContainText('WF5')
  await expect(detail).not.toContainText('WF4')
  await expect(detail).not.toContainText('ADMITTED')
  await expect(detail).toHaveAttribute('title', /Full saved dependency explanation/)
  await expect(detail).not.toContainText('Full saved dependency explanation')
  await page.getByRole('button', { name: 'Show jobs for WF4', exact: true }).click()
  const jobRow = page.getByRole('row').filter({ has: page.getByText('WF4 job', { exact: true }) })
  await expect(jobRow.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(jobRow).not.toContainText('Full saved dependency explanation')
  pgOnly(state)
})

test('an idle WF4 stays NO RUN while its manual button explains direct parent and child exclusions', async ({ page }) => {
  const state = await setup(page)
  const idle = row('wf4', 'WF4')
  idle.canRun = false
  idle.runUnavailable = 'Waiting for parent WF2 (running) to complete; Waiting for child WF5 (running) to complete'
  state.data.rows.push(idle)
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const workflow = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF4', exact: true }) })
  await expect(workflow.locator('.om-execution-status')).toContainText('NO RESULT')
  await expect(workflow.locator('.om-planning-cell')).not.toContainText('WF5')
  await expect(workflow.locator('.om-name-detail')).not.toHaveClass(/om-error/)
  const button = page.getByRole('button', { name: 'Run workflow WF4', exact: true })
  await expect(button).toBeDisabled()
  await expect(button).toHaveAttribute('title', /parent WF2.*child WF5/)
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('Monitor creates and pauses PostgreSQL workflow schedules with revision checks', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Actions for workflow WF1', exact: true }).click()
  await page.getByRole('button', { name: 'Enable schedule', exact: true }).click()
  await page.getByLabel('Cron expression', { exact: true }).fill('* * * * *')
  await page.getByLabel('Schedule timezone', { exact: true }).fill('Europe/Stockholm')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(state.writes[0]).toMatchObject({ path: prefix + '/schedules', body: { enabled: true, target: { graphId: 'graph', workflowId: wf }, cron: '* * * * *' } })
  await page.getByRole('button', { name: 'Actions for workflow WF1', exact: true }).click()
  await page.getByRole('button', { name: 'Disable schedule', exact: true }).click()
  await expect(page.getByText('WF1 schedule disabled.', { exact: true })).toBeVisible()
  expect(state.writes[1]).toMatchObject({ path: prefix + '/schedules/schedule', body: { enabled: false, expectedRevision: 1 } })
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

test('latest SUCCESS remains visible beside a newer BLOCKED plan with matching jobs and log', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0]
  workflow.run = { status: 'BLOCKED', runId: 'never-started-request', start: null,
    message: 'Parent WF2 completed before the child last started',
    waitingFor: [{ name: 'WF2', status: 'STALE_PARENT', relationship: 'PARENT' }] }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  await expect(display.locator('.om-execution-status')).toHaveText(/SUCCESS/)
  await expect(display.locator('.om-planning-cell')).toContainText('BLOCKED')
  await expect(display.locator('.om-planning-cell')).toContainText('Parent WF2STALE')
  await expect(display).toContainText('1m 00s')
  await expect(display.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
  await page.getByRole('button', { name: 'Show jobs for WF1', exact: true }).click()
  const jobRow = page.getByRole('row').filter({ has: page.getByText('WF1 job', { exact: true }) })
  await expect(jobRow.locator('.om-execution-status')).toHaveText(/SUCCESS/)
  await page.getByLabel('Filter workflow result', { exact: true }).selectOption('SUCCESS')
  await page.getByLabel('Filter workflow activity', { exact: true }).selectOption('BLOCKED')
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run workflow WF2', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Log for workflow WF1', exact: true }).click()
  await expect.poll(() => state.requests.includes(`${prefix}/workflows/${wf}/runs/${execution}/log`)).toBe(true)
  expect(state.requests.some(path => path.includes('never-started-request/log'))).toBe(false)
  pgOnly(state)
})

test('pending retry retains FAILED execution and scheduled idle work retains its last result', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0]
  workflow.lastRun = { ...workflow.lastRun, status: 'FAILED' }
  workflow.run = { status: 'WAITING', runId: 'retry-request', start: null, message: 'Waiting for child WF2', waitingFor: [{ name: 'WF2', status: 'RUNNING', relationship: 'CHILD' }] }
  workflow.waitingCount = 2; workflow.canRun = false
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  await expect(display.locator('.om-execution-status')).toHaveText(/FAILED/)
  await expect(display.locator('.om-planning-cell')).toContainText('PLANNED')
  workflow.run = workflow.lastRun; workflow.waitingCount = 0; workflow.IS_SCHEDULE_ENABLED = true
  workflow.SCHEDULE = '* * * * *'
  workflow.scheduler = { state: 'STARTED', nextRun: '2026-11-23T14:01:00Z' }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(display.locator('.om-execution-status')).toHaveText(/FAILED/)
  await expect(display.locator('.om-planning-cell')).toBeEmpty()
  await expect(display).toContainText('15:01:00')
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
  expect(requests).not.toContain('/api/snowflake/ping')
})

test('discovery recovers automatically after a backend interruption and restores PostgreSQL presence', async ({ page }) => {
  const state = await setup(page, { healthUnavailable: true, waitForMonitor: false })
  await expect(page.getByRole('alert')).toContainText('Retrying automatically')
  expect(state.requests.filter(path => path.startsWith('/api/orchestration/'))).toEqual([])
  expect(state.requests).not.toContain('/api/snowflake/ping')
  state.healthUnavailable = false
  await page.clock.runFor(3500)
  await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  const urls = await page.evaluate(() => window.__eventUrls)
  expect(urls.length).toBeGreaterThan(0)
  expect(urls.every(url => url.includes('orchestrationSource=POSTGRES'))).toBe(true)
  expect(state.requests).not.toContain('/api/snowflake/ping')
  pgOnly(state)
})

test('discovery can be retried immediately without reloading the page', async ({ page }) => {
  const state = await setup(page, { healthUnavailable: true, waitForMonitor: false })
  await expect(page.getByRole('alert')).toContainText('Backend temporarily unavailable')
  state.healthUnavailable = false
  await page.getByRole('button', { name: 'Retry now', exact: true }).click()
  await expect(page.getByLabel('Configuration source', { exact: true })).toHaveValue('POSTGRES')
  await expect(page.getByRole('button', { name: 'Run workflow WF1', exact: true })).toBeVisible()
  expect(state.requests).not.toContain('/api/snowflake/ping')
  pgOnly(state)
})

test('five columns show separate plan requests and clear completed plans without erasing Last run', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0]
  const request = (id, parent, time) => ({ runId: id, requestId: id, cascadeId: id + '-plan', planStatus: 'RUNNING',
    status: 'WAITING', requestedAt: time, requestedBy: 'Tester', start: null, end: null,
    waitingFor: [{ name: parent, relationship: 'PARENT', status: 'RUNNING' }],
    trigger: { kind: 'PARENT', parents: [{ name: parent, status: 'RUNNING' }] },
    jobs: [{ jobId: job, status: 'PENDING', type: 'SQL', source: 'POSTGRES', start: null, end: null }] })
  workflow.requests = [request('first', 'Parent A', stamp), request('second', 'Parent B', '2026-11-23T14:01:00Z')]
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(page.getByRole('columnheader')).toHaveText(['Workflow', 'Status', 'Last run', 'Duration', 'Trigger'])
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  const status = display.locator('.om-planning-cell')
  await expect(status.locator('.om-request')).toHaveCount(2)
  await expect(status.locator('[data-request-id="first"]')).toContainText('Parent A')
  await expect(status.locator('[data-request-id="first"]')).not.toContainText('Parent B')
  await expect(status.locator('[data-request-id="second"]')).toContainText('Parent B')
  await expect(display.locator('.om-last-run')).toContainText('14:58:00')
  await expect(display.locator('.om-last-run')).toContainText('SUCCESS')
  await page.getByRole('button', { name: 'Show jobs for WF1', exact: true }).click()
  const child = page.locator('#om-jobs-' + wf).getByRole('row').first()
  await expect(child.locator('.om-request')).toHaveCount(2)
  workflow.requests[0].status = 'SUCCESS'; workflow.requests[0].jobs[0].status = 'SUCCESS'
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(status.locator('[data-request-id="first"]')).toContainText('SUCCEEDED')
  await expect(child.locator('[data-request-id="first"]')).toContainText('SUCCEEDED')
  workflow.requests[0].planStatus = 'SUCCESS'
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(status.locator('.om-request')).toHaveCount(1)
  await expect(status).toContainText('Parent B')
  await expect(status).not.toContainText('Parent A')
  workflow.requests[1].planStatus = 'FAILED'
  workflow.IS_SCHEDULE_ENABLED = true; workflow.SCHEDULE = '0 7 * * 1-5'
  workflow.scheduler = { state: 'STARTED', nextRun: '2026-11-24T06:00:00Z' }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(status).toBeEmpty()
  await expect(child.locator('.om-planning-cell')).toBeEmpty()
  await expect(display.locator('.om-last-run')).toContainText('14:58:00')
  await expect(display.locator('.om-last-run')).toContainText('SUCCESS')
  await expect(display.locator('.om-triggers')).toContainText('Next run')
  await page.getByLabel('Filter workflow activity', { exact: true }).selectOption('NO PLAN')
  await expect(display).toBeVisible()
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('Last run timing stays attached to its result while duration and progress follow the active request', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0]
  workflow.requests = [{ runId: 'current', requestId: 'current', planStatus: 'RUNNING', status: 'RUNNING',
    start: stamp, end: null, requestedAt: stamp, trigger: { kind: 'MANUAL' },
    jobs: [{ jobId: job, status: 'SUCCESS' }, { jobId: 'another', status: 'RUNNING' }] }]
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  await expect(display.locator('.om-last-run')).toContainText('14:58:00')
  await expect(display.locator('.om-last-run')).not.toContainText('15:00:00')
  await expect(display.locator('.om-last-run')).toContainText('SUCCESS')
  await expect(display.locator('.om-durations')).toContainText('Last: 1m 00s')
  await expect(display.locator('.om-durations')).toContainText('Current: 0m')
  await expect(display.locator('.om-planning-cell')).toContainText('STARTED')
  await expect(display.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50')
  const elapsed = text => { const match = text.match(/Current: (\d+)m (\d+)s/); return Number(match[1]) * 60 + Number(match[2]) }
  const before = elapsed(await display.locator('.om-durations').innerText())
  await page.clock.runFor(5000)
  await expect.poll(async () => elapsed(await display.locator('.om-durations').innerText())).toBeGreaterThanOrEqual(before + 5)
  pgOnly(state)
})

test('Trigger shows current parent and paused schedule configuration while Manual belongs to Last run', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0]
  workflow.configuredParents = [{ id: 'configured-parent', name: 'Configured parent', success: true, warning: false, error: false, parentMustBeNewer: true, enabled: true }]
  workflow.SCHEDULE = '0 7 * * 1-5'; workflow.SCHEDULE_TIMEZONE = 'Europe/Stockholm'
  workflow.lastResult = { ...workflow.run, triggerSource: 'MANUAL' }
  workflow.jobs[0].lastResult = { ...workflow.jobs[0].run, triggerSource: 'MANUAL' }
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  const trigger = display.locator('.om-triggers')
  await expect(display.locator('.om-planning-cell')).toBeEmpty()
  await expect(trigger).toContainText('Parent Configured parent')
  await expect(trigger).toContainText('On success')
  await expect(trigger).toContainText('Newer result required')
  await expect(trigger).toContainText('0 7 * * 1-5')
  await expect(trigger).toContainText('Europe/Stockholm · Paused')
  await expect(trigger).not.toContainText('Next run')
  await expect(trigger).not.toContainText('Manual')
  await expect(display.locator('.om-last-run .om-manual-trigger')).toHaveText('Manual')
  await page.getByRole('button', { name: 'Show jobs for WF1', exact: true }).click()
  const child = page.locator('#om-jobs-' + wf).getByRole('row').first()
  await expect(child.locator('.om-last-run .om-manual-trigger')).toHaveText('Manual')
  await expect(child.locator('.om-triggers')).toContainText('With workflow')
  await expect(child.locator('.om-triggers')).not.toContainText('Manual')
  // A new manual request must not relabel the previous scheduled result.
  workflow.lastResult.triggerSource = 'SCHEDULED'
  workflow.jobs[0].lastResult.triggerSource = 'PARENT'
  workflow.requests = [{ runId: 'new-manual', requestId: 'new-manual', planStatus: 'QUEUED', status: 'WAITING', requestedAt: stamp,
    trigger: { kind: 'MANUAL', parents: [{ name: 'Old snapshot parent', status: 'RUNNING' }] }, jobs: [] }]
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(display.locator('.om-planning-cell')).toContainText('PLANNED')
  await expect(display.locator('.om-last-run')).toContainText('SUCCESS')
  await expect(display.locator('.om-last-run')).not.toContainText('Manual')
  await expect(child.locator('.om-last-run')).not.toContainText('Manual')
  await expect(trigger).toContainText('Configured parent')
  await expect(trigger).not.toContainText('Old snapshot parent')
  expect(state.writes).toEqual([])
  pgOnly(state)
})

test('consecutive plans keep one workflow status and parent status per plan', async ({ page }) => {
  const state = await setup(page, { started: true })
  const workflow = state.data.rows[0]
  const parent = { name: 'WF8', nodeKey: 'wf8', relationship: 'PARENT' }
  workflow.requests = [
    { runId: 'old-occurrence', requestId: 'old-request', cascadeId: '11111111-old', planStatus: 'RUNNING', status: 'RUNNING', start: stamp,
      jobs: [{ jobId: job, status: 'RUNNING', start: stamp }] },
    { runId: 'new-occurrence', requestId: 'new-request', cascadeId: '22222222-new', planStatus: 'QUEUED', status: 'WAITING', requestedAt: stamp,
      waitingFor: [{ ...parent, status: 'RUNNING', cascadeId: '11111111-old', overlap: true },
        { ...parent, status: 'WAITING', cascadeId: '22222222-new', overlap: false },
        { name: 'WF9', relationship: 'CHILD', status: 'RUNNING', cascadeId: '11111111-old', overlap: true }],
      jobs: [{ jobId: job, status: 'PENDING' }] }
  ]
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  const display = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Execution history for WF1', exact: true }) })
  const previous = display.locator('[data-plan-id="11111111-old"]')
  const current = display.locator('[data-plan-id="22222222-new"]')
  await expect(display.locator('.om-request')).toHaveCount(2)
  await expect(previous.locator('.om-request-heading .status-badge')).toHaveCount(1)
  await expect(previous.locator('.om-request-heading')).toContainText('STARTED')
  await expect(current.locator('.om-request-heading .status-badge')).toHaveCount(1)
  await expect(current.locator('.om-request-heading')).toContainText('PLANNED')
  await expect(current.locator('.om-dependency-item')).toHaveCount(1)
  await expect(current.locator('.om-dependency-item .status-badge')).toHaveCount(1)
  await expect(current.locator('.om-dependency-item')).toContainText('Parent WF8WAITING')
  await expect(current).not.toContainText('RUNNING')
  await expect(current).not.toContainText('WF9')
  await expect(current.locator('.om-plan-overlap')).toHaveText('Waiting for plan 11111111 · WF8')
  await expect(current.locator('.om-request-meta')).toContainText('Plan 22222222')
  await page.getByRole('button', { name: 'Show jobs for WF1', exact: true }).click()
  const child = page.locator('#om-jobs-' + wf).getByRole('row').first()
  await expect(child.locator('[data-plan-id="22222222-new"] .om-request-heading .status-badge')).toHaveCount(1)
  await expect(child.locator('[data-plan-id="22222222-new"] .om-dependency-item')).toContainText('Parent WF8WAITING')
  workflow.requests[0].planStatus = 'SUCCESS'
  workflow.requests[1].waitingFor = [{ ...parent, status: 'WAITING', cascadeId: '22222222-new' }]
  await page.getByRole('button', { name: 'Refresh', exact: false }).first().click()
  await expect(previous).toHaveCount(0)
  await expect(display.locator('.om-request')).toHaveCount(1)
  await expect(current.locator('.om-request-meta')).toContainText('Plan 22222222')
  await expect(current.locator('.om-plan-overlap')).toHaveCount(0)
  await expect(current.locator('.om-request-heading')).toContainText('PLANNED')
  expect(state.writes).toEqual([])
  pgOnly(state)
})
