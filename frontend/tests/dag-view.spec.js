import { test, expect } from '@playwright/test'

const prefix = '/api/orchestration/task-engine'
const workflows = ['WF1', 'WF2', 'WF3', 'WF4', 'WF5', 'Independent'].map((name, index) => ({
  WORKFLOW_ID: name, WORKFLOW_NAME: name, WORKFLOW_GROUP: index < 2 ? 'Alpha' : 'Beta', IS_ENABLED: true, JOB_EXECUTION_MODE: 'SEQUENTIAL'
}))
const dependencies = [['WF1', 'WF2'], ['WF1', 'WF3'], ['WF2', 'WF4'], ['WF3', 'WF4'], ['WF4', 'WF5']]
  .map(([parent, child], index) => ({ DEPENDENCY_ID: String(index), PARENT_WORKFLOW_ID: parent, CHILD_WORKFLOW_ID: child, ALLOW: true, SUCCESS: true, WARNING: false, ERROR: false }))
const jobs = workflows.flatMap(w => [1, 2].map(index => ({ WORKFLOW_ID: w.WORKFLOW_ID, JOB_ID: w.WORKFLOW_ID + index,
  JOB_NAME: `${w.WORKFLOW_NAME} job ${index}`, JOB_TYPE: w.WORKFLOW_ID === 'WF4' && index === 2 ? 'DBT' : 'SQL',
  DBT_COMMAND: w.WORKFLOW_ID === 'WF4' && index === 2 ? 'dbt build --select downstream' : null,
  JOB_SEQUENCE: index, JOB_IS_ENABLED: true, PARENT_JOB_ID: index === 2 ? w.WORKFLOW_ID + '1' : null })))

async function setup(page) {
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const state = { requests: [], writes: [], errors: [], rejectMonitor: false, rejectPlan: false, revision: 1, worker: 'HEALTHY', rows: workflows.map(w => ({ ...w,
    source: 'POSTGRES', graphId: 'graph', configurationRevision: 1, canRun: true, canRunWithChildren: true,
    run: { status: 'SUCCESS' }, lastResult: { status: 'SUCCESS' }, planning: { status: 'NO PLAN', waitingCount: 0, waitingFor: [] },
    jobs: jobs.filter(j => j.WORKFLOW_ID === w.WORKFLOW_ID).map(j => ({ ...j, run: { status: 'SUCCESS' }, lastResult: { status: 'SUCCESS' } }))
  })) }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname
    state.requests.push(path)
    let body = { ok: true }
    if (request.method() !== 'GET') {
      expect(path).toBe(prefix + '/graphs/graph/runs')
      const payload = request.postDataJSON()
      state.writes.push(payload)
      const row = state.rows.find(r => r.WORKFLOW_ID === payload.workflowId)
      row.run.status = 'QUEUED'; row.canRun = false; row.planning = { status: 'QUEUED', waitingCount: 0 }
      return route.fulfill({ json: { ok: true, cascadeId: 'run-group' } })
    }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', taskPrototypeEnabled: true, postgresEnginePrimary: true, legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === prefix + '/flow') body = { ok: true, source: 'PostgreSQL', configurationStore: 'POSTGRES', graphId: 'graph', configurationRevision: state.revision, workflows, dependencies, jobs }
    if (path === prefix + '/monitor') {
      if (state.rejectMonitor) return route.fulfill({ status: 503, json: { error: 'Database disconnected' } })
      body = { ok: true, rows: state.rows, submissions: [], worker: { state: state.worker }, runsEnabled: true, configurationRevision: state.revision, generatedAt: new Date().toISOString() }
    }
    if (path === prefix + '/graphs/graph/plan') {
      if (state.rejectPlan) return route.fulfill({ status: 503, json: { error: 'Plan temporarily unavailable' } })
      const root = url.searchParams.get('workflowId'), selected = new Set([root]), pending = url.searchParams.get('includeChildren') === 'false' ? [] : [root]
      while (pending.length) {
        const parent = pending.pop()
        for (const edge of dependencies.filter(edge => edge.PARENT_WORKFLOW_ID === parent)) {
          if (!selected.has(edge.CHILD_WORKFLOW_ID)) { selected.add(edge.CHILD_WORKFLOW_ID); pending.push(edge.CHILD_WORKFLOW_ID) }
        }
      }
      body = { ok: true, rootKey: root, issues: [], omitted: [], nodes: workflows.filter(w => selected.has(w.WORKFLOW_ID)).map(w => ({
        key: w.WORKFLOW_ID, workflowId: w.WORKFLOW_ID, name: w.WORKFLOW_NAME, jobs: [], issues: [],
        parents: w.WORKFLOW_ID === root ? [] : dependencies.filter(e => e.CHILD_WORKFLOW_ID === w.WORKFLOW_ID).map(e => ({ key: e.PARENT_WORKFLOW_ID, name: e.PARENT_WORKFLOW_ID, allow: true, success: true, warning: false, error: false, inPlan: selected.has(e.PARENT_WORKFLOW_ID) }))
      })) }
    }
    return route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationFlow')
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(6)
  await expect(node(page, 'WF1').getByRole('button', { name: 'Run WF1', exact: true })).toBeEnabled()
  return state
}

const node = (page, id) => page.locator(`.react-flow__node-workflow[data-id="${id}"]`)
const position = node => node.evaluate(el => el.style.transform.match(/translate\(([-\d.]+)px, ([-\d.]+)px\)/).slice(1).map(Number))
const tick = page => page.clock.runFor(2100)
const zoom = page => page.locator('.react-flow__viewport').evaluate(el => Number(el.style.transform.match(/scale\(([\d.]+)\)/)[1]))
async function expectCentered(page, id) {
  await expect.poll(async () => {
    const card = await node(page, id).boundingBox()
    const canvas = await page.locator('.react-flow').boundingBox()
    return Math.max(Math.abs(card.x + card.width / 2 - canvas.x - canvas.width / 2),
      Math.abs(card.y + card.height / 2 - canvas.y - canvas.height / 2))
  }).toBeLessThan(2)
}

test('name, group and selection show both ancestors and descendants using PostgreSQL only', async ({ page }) => {
  const state = await setup(page)
  await expect(page.getByRole('heading', { name: 'DAG View', exact: true })).toBeVisible()
  await page.getByLabel('Filter workflow group', { exact: true }).selectOption('Alpha')
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(5)
  await expect(node(page, 'WF5')).toBeVisible()
  await expect(node(page, 'Independent')).toHaveCount(0)
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click()
  await page.getByLabel('Filter workflows by name or group').fill('beta')
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(6)
  await page.getByLabel('Filter workflows by name or group').fill('WF2')
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(5)
  for (const id of ['WF1', 'WF2', 'WF3', 'WF4', 'WF5']) await expect(node(page, id)).toBeVisible()
  await node(page, 'WF4').locator('strong').first().click()
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(5)
  await expect(node(page, 'WF3')).toBeVisible()
  await expect(page.locator('.workflow-flow-selection')).toContainText('Dependencies: WF4')
  await expect(page.locator('.react-flow__edge[data-id="workflow-2"]')).toHaveCount(1)
  await page.getByRole('button', { name: 'Back to overview' }).click()
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(5)
  expect(state.requests.filter(path => path.endsWith('/plan'))).toEqual([])
  expect(state.requests.filter(path => path.startsWith('/api/orchestration/') && !path.startsWith(prefix))).toEqual([])
  expect(state.writes).toEqual([])
  expect(state.errors).toEqual([])
})

test('selection rebuilds the graph and job expansion recalculates dimensions', async ({ page }) => {
  const state = await setup(page)
  const initialZoom = await zoom(page)
  await node(page, 'Independent').locator('strong').first().click()
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(1)
  expect(await position(node(page, 'Independent'))).toEqual([30, 30])
  await expectCentered(page, 'Independent')
  expect(await zoom(page)).toBeCloseTo(initialZoom, 8)
  await page.getByRole('button', { name: 'Back to overview' }).click()
  await node(page, 'WF4').locator('strong').first().click()
  await expect(page.locator('.react-flow__node-workflow')).toHaveCount(5)
  const childBefore = await position(node(page, 'WF5'))
  await node(page, 'WF4').getByRole('button', { name: 'Expand WF4 jobs' }).click()
  await expect(page.locator('.react-flow__node-job')).toHaveCount(2)
  await expect(page.locator('[data-id="WF4::WF42"] .workflow-flow-job')).toHaveAttribute('title', 'dbt build --select downstream')
  await expect(page.locator('[data-id="WF4::WF42"]')).toContainText('DBT · After WF4 job 1')
  await expect(node(page, 'WF4')).toHaveCSS('height', '346px')
  expect((await position(node(page, 'WF5')))[1]).not.toBe(childBefore[1])
  await node(page, 'WF4').getByRole('button', { name: 'Collapse WF4 jobs' }).click()
  await expect(page.locator('.react-flow__node-job')).toHaveCount(0)
  await expect(node(page, 'WF4')).toHaveCSS('height', '152px')
  expect(state.errors).toEqual([])
})

test('live workflow and job updates retain last results, positions and zoom', async ({ page }) => {
  await page.clock.install()
  const state = await setup(page)
  await node(page, 'WF4').getByRole('button', { name: 'Expand WF4 jobs' }).click()
  await page.getByRole('button', { name: 'Zoom In', exact: true }).click()
  await page.clock.runFor(300)
  const handle = await node(page, 'WF4').locator('.workflow-flow-group-icon').boundingBox()
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2)
  await page.mouse.down()
  await page.mouse.move(handle.x + handle.width / 2 + 35, handle.y + handle.height / 2 + 35, { steps: 8 })
  await page.mouse.up()
  const before = await position(node(page, 'WF4'))
  const viewport = await page.locator('.react-flow__viewport').getAttribute('style')
  const row = state.rows.find(w => w.WORKFLOW_ID === 'WF4')
  row.run.status = 'RUNNING'; row.canRun = false
  row.planning = { status: 'RUNNING', waitingCount: 2 }
  row.jobs[0].run.status = 'RUNNING'; row.jobs[1].run.status = 'PENDING'
  await tick(page)
  await expect(node(page, 'WF4').getByLabel('State', { exact: true })).toContainText('RUNNING')
  await expect(node(page, 'WF4').getByLabel('State', { exact: true })).toContainText('WAITING(2)')
  await expect(node(page, 'WF4').getByLabel('Last result')).toHaveText('Last resultSUCCESS')
  await expect(page.locator('[data-id="WF4::WF41"]').getByLabel('State', { exact: true })).toContainText('RUNNING')
  await expect(page.locator('[data-id="WF4::WF42"]').getByLabel('State', { exact: true })).toContainText('WAITING')
  await expect(node(page, 'WF4').getByRole('button', { name: 'Run WF4', exact: true })).toBeDisabled()
  expect(await position(node(page, 'WF4'))).toEqual(before)
  expect(await page.locator('.react-flow__viewport').getAttribute('style')).toEqual(viewport)
  row.run.status = 'FAILED'; row.lastResult.status = 'FAILED'; row.planning = { status: 'NO PLAN' }; row.canRun = true
  row.jobs[0].run.status = 'FAILED'; row.jobs[0].lastResult.status = 'FAILED'
  await tick(page)
  await expect(node(page, 'WF4').getByLabel('Last result')).toContainText('FAILED')
  await expect(node(page, 'WF4').getByRole('button', { name: 'Run WF4', exact: true })).toBeEnabled()
  expect(state.requests.filter(path => path.endsWith('/flow'))).toHaveLength(1)
  expect(state.errors).toEqual([])
})

test('Run previews the actual scope and submits once without rerunning displayed ancestors', async ({ page }) => {
  const state = await setup(page)
  await node(page, 'WF4').locator('strong').first().click()
  await node(page, 'WF4').getByRole('button', { name: 'Run WF4', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByLabel('Execution plan')).toContainText('WF4')
  await expect(dialog.getByLabel('Execution plan')).not.toContainText('WF1')
  await page.getByLabel('Execution scope').selectOption('WORKFLOW')
  await expect(dialog.getByLabel('Execution plan')).not.toContainText('WF5')
  await dialog.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(node(page, 'WF4').getByLabel('State', { exact: true })).toContainText('QUEUED')
  expect(state.writes).toHaveLength(1)
  expect(state.writes[0]).toMatchObject({ workflowId: 'WF4', includeChildren: false })
  expect(state.writes[0].requestId).toBeTruthy()
  expect(state.errors).toEqual([])
})

test('live connection loss blocks starts including an open dialog and recovers automatically', async ({ page }) => {
  await page.clock.install()
  const state = await setup(page)
  await node(page, 'WF4').getByRole('button', { name: 'Run WF4', exact: true }).click()
  const submit = page.getByRole('dialog').getByRole('button', { name: 'Run workflow', exact: true })
  await expect(submit).toBeEnabled()
  state.rejectMonitor = true
  await tick(page)
  await expect(page.getByRole('alert')).toContainText('Database disconnected')
  await expect(submit).toBeDisabled()
  state.rejectMonitor = false
  await tick(page)
  await expect(submit).toBeEnabled()
  await page.getByRole('button', { name: 'Close dialog' }).click()
  state.worker = 'UNAVAILABLE'
  await tick(page)
  await expect(node(page, 'WF4').getByRole('button', { name: 'Run WF4', exact: true })).toBeDisabled()
  expect(state.writes).toEqual([])
  expect(state.errors).toEqual([])
})

test('waiting state shows only direct parent details and configuration changes reload the catalog once', async ({ page }) => {
  await page.clock.install()
  const state = await setup(page)
  const row = state.rows.find(w => w.WORKFLOW_ID === 'WF4')
  row.planning = { status: 'WAITING', waitingCount: 2, waitingFor: [
    { relationship: 'PARENT', name: 'WF2', status: 'RUNNING' },
    { relationship: 'PARENT', name: 'WF3', status: 'STALE_PARENT' },
    { relationship: 'CHILD', name: 'WF5', status: 'RUNNING' }
  ] }
  row.canRun = false
  state.revision = 2
  await tick(page)
  await expect(node(page, 'WF4').getByLabel('State', { exact: true })).toContainText('WAITING(2)')
  await expect(node(page, 'WF4').locator('.workflow-flow-run-row')).toContainText('WF2 (RUNNING)')
  await expect(node(page, 'WF4').locator('.workflow-flow-run-row')).not.toContainText('WF5')
  await expect.poll(() => state.requests.filter(path => path.endsWith('/flow')).length).toBe(2)
  await tick(page)
  expect(state.requests.filter(path => path.endsWith('/flow'))).toHaveLength(2)
  expect(state.errors).toEqual([])
})

test('clicking workflows centers them at the same zoom and preserves dragged positions', async ({ page }) => {
  await page.clock.install()
  const state = await setup(page)
  await page.getByLabel('Filter workflows by name or group').fill('WF2')
  await node(page, 'WF2').locator('strong').first().click()
  await page.clock.runFor(300)
  await expectCentered(page, 'WF2')
  await expect(node(page, 'WF3')).toBeVisible()
  await page.getByRole('button', { name: 'Zoom In', exact: true }).click()
  await page.clock.runFor(300)
  const handle = await node(page, 'WF2').locator('.workflow-flow-group-icon').boundingBox()
  await page.mouse.move(handle.x + 8, handle.y + 8)
  await page.mouse.down()
  await page.mouse.move(handle.x + 33, handle.y + 33, { steps: 8 })
  await page.mouse.up()
  const initialZoom = await zoom(page)
  const moved = await position(node(page, 'WF2'))
  await node(page, 'WF3').locator('strong').first().click()
  await expect(page.locator('.workflow-flow-selection')).toContainText('Dependencies: WF3')
  await page.clock.runFor(300)
  await expectCentered(page, 'WF3')
  expect(await zoom(page)).toBeCloseTo(initialZoom, 8)
  expect(await position(node(page, 'WF2'))).toEqual(moved)
  await expect(node(page, 'WF2')).toBeVisible()
  await expect(node(page, 'WF1')).toHaveCount(1)
  await expect(node(page, 'WF4')).toHaveCount(1)
  await expect(node(page, 'Independent')).toHaveCount(0)
  await node(page, 'WF2').locator('strong').first().click()
  await expect(page.locator('.workflow-flow-selection')).toContainText('Dependencies: WF2')
  await page.clock.runFor(300)
  await expectCentered(page, 'WF2')
  expect(await zoom(page)).toBeCloseTo(initialZoom, 8)
  const canvas = await page.locator('.react-flow').boundingBox()
  await page.mouse.move(canvas.x + 80, canvas.y + 80)
  await page.mouse.down()
  await page.mouse.move(canvas.x + 150, canvas.y + 130, { steps: 6 })
  await page.mouse.up()
  // Clicking the already selected card must also re-center after panning.
  await node(page, 'WF2').locator('strong').first().click()
  await page.clock.runFor(300)
  await expectCentered(page, 'WF2')
  expect(await zoom(page)).toBeCloseTo(initialZoom, 8)
  const centered = await page.locator('.react-flow__viewport').getAttribute('style')
  await tick(page)
  expect(await page.locator('.react-flow__viewport').getAttribute('style')).toEqual(centered)
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('card backgrounds distinguish results and current activity even when selected', async ({ page }) => {
  await page.clock.install()
  const state = await setup(page)
  await page.getByLabel('Filter workflows by name or group').fill('WF4')
  const row = id => state.rows.find(w => w.WORKFLOW_ID === id)
  row('WF2').lastResult.status = 'WARNING'
  row('WF3').lastResult.status = 'FAILED'
  row('WF4').planning.status = 'RUNNING'
  row('WF4').run.status = 'RUNNING'
  row('WF4').lastResult.status = 'FAILED'
  row('WF5').planning.status = 'WAITING'
  row('WF5').planning.waitingCount = 1
  await tick(page)
  const card = id => node(page, id).locator('.workflow-flow-group')
  await expect(card('WF1')).toHaveCSS('background-color', 'rgb(18, 63, 48)')
  await expect(card('WF2')).toHaveCSS('background-color', 'rgb(105, 81, 29)')
  await expect(card('WF3')).toHaveCSS('background-color', 'rgb(102, 37, 45)')
  await expect(card('WF4')).toHaveCSS('background-color', 'rgb(23, 74, 128)')
  await expect(card('WF5')).toHaveCSS('background-color', 'rgb(52, 48, 77)')
  await node(page, 'WF3').locator('strong').first().click()
  await expect(card('WF3')).toHaveCSS('background-color', 'rgb(102, 37, 45)')
  await expect(card('WF3')).toHaveCSS('outline-style', 'solid')
  await node(page, 'WF4').locator('strong').first().click()
  await expect(card('WF4')).toHaveCSS('background-color', 'rgb(23, 74, 128)')
  await expect(node(page, 'WF4').getByLabel('Last result')).toContainText('FAILED')
  row('WF4').planning.status = 'NO PLAN'; row('WF4').run.status = 'SUCCESS'; row('WF4').lastResult.status = 'SUCCESS'
  await tick(page)
  await expect(card('WF4')).toHaveCSS('background-color', 'rgb(18, 63, 48)')
  await expect(node(page, 'WF4').getByLabel('Last result')).toContainText('SUCCESS')
  expect(state.errors).toEqual([])
})
