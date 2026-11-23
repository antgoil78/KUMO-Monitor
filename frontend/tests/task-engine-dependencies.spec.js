import { test, expect } from '@playwright/test'

async function setup(page, { failStart = false } = {}) {
  await page.clock.install({ time: new Date('2026-11-23T14:00:00Z') })
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const definitions = [['WF1', []], ['WF2', ['WF1']], ['WF3', ['WF1']], ['WF4', ['WF2', 'WF3']]].map(([key, parents]) => ({ key, name: key,
    plan: { jobs: [{ key: 'sql', sql: 'SELECT 1' }] }, parents: parents.map(key => ({ key, parentMustBeNewer: key !== 'WF3' })) }))
  const graph = { id: 'graph-1', name: 'WF1 SQL dependency trial', revision: 1, enabled: true, definition: { nodes: definitions } }
  const state = { graph, mutations: [], cascade: null, nodes: [], dispatch: { state: 'DONE', error: null }, outcome: 'RUNNING' }
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', taskPrototypeEnabled: true, legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/orchestration/monitor') body = { ok: true, rows: [], submissions: [], runsEnabled: true }
    if (path === '/api/orchestration/task-engine') body = { ok: true, counts: {}, runs: [], cleanupRetrying: 0 }
    if (path === '/api/orchestration/task-engine/graphs') body = { ok: true, graphs: [graph], cascades: state.cascade ? [state.cascade] : [], dispatcher: { state: 'HEALTHY' } }
    if (path === '/api/orchestration/task-engine/graphs/graph-1/runs') {
      state.mutations.push({ path, body: req.postDataJSON() })
      if (failStart && state.mutations.length === 1) return route.fulfill({ status: 503, json: { error: 'Admission response unavailable' } })
      state.cascade = { id: 'cascade-1', graph_id: 'graph-1', graph_revision: 1, request_id: 'trial-request', state: 'RUNNING', snapshot: { nodes: definitions } }
      graph.active_cascade = { id: 'cascade-1', state: 'RUNNING' }
      state.nodes = definitions.map(n => ({ node_key: n.key, state: n.key === 'WF1' ? 'ADMITTED' : 'WAITING', run_id: n.key === 'WF1' ? 'run-WF1' : null,
        run_state: n.key === 'WF1' ? 'RUNNING' : null, blockers: n.parents.map(p => ({ parent: p.key, state: 'ADMITTED', reason: 'Waiting for parent completion' })), parent_runs: {} }))
      body = { ok: true, cascadeId: 'cascade-1' }
    }
    if (path === '/api/orchestration/task-engine/cascades/cascade-1') body = { ok: true, cascade: state.cascade, nodes: state.nodes, dispatch: state.dispatch }
    if (path === '/api/orchestration/task-engine/cascades/cascade-1/retry-dispatch') {
      state.mutations.push({ path }); state.dispatch = { state: 'DONE', error: null }; body = { ok: true }
    }
    if (path === '/api/orchestration/task-engine/cascades/cascade-1/cancel') {
      state.mutations.push({ path }); state.cascade.cancel_requested = true; body = { ok: true }
    }
    if (path.startsWith('/api/orchestration/task-engine/runs/run-WF')) body = { ok: true,
      run: { id: path.split('/').at(-1), state: state.outcome }, jobs: [{ id: 'sql', job_key: 'source SQL', state: state.outcome, cleanup_state: 'DONE' }] }
    return route.fulfill({ json: body })
  })
  await page.goto('/?page=orchestrationMonitor')
  const region = page.getByRole('region', { name: 'Workflow dependency trial' })
  await expect(region).toContainText('HEALTHY')
  return { state, region }
}

async function start(region) {
  await region.getByRole('button', { name: 'Run WF1', exact: true }).click()
  await expect(region.getByRole('table', { name: 'Workflow dependency status' })).toBeVisible()
}

test('WF1 starts only on request and shows copied parent freshness rules', async ({ page }) => {
  const { state, region } = await setup(page)
  expect(state.mutations).toHaveLength(0)
  await start(region)
  await expect(region.getByRole('button', { name: 'Run WF1', exact: true })).toBeDisabled()
  const wf4 = region.getByRole('table').getByRole('row').filter({ hasText: 'WF4' })
  await expect(wf4).toContainText('WF2 (fresh), WF3')
  await expect(wf4).toContainText('No Snowflake task admitted')
  expect(Object.keys(state.mutations[0].body)).toEqual(['requestId'])
})

test('join waits visibly for WF3 and job inspection never submits another workflow', async ({ page }) => {
  const { state, region } = await setup(page)
  await start(region)
  state.nodes.find(n => n.node_key === 'WF1').run_state = 'SUCCESS'
  Object.assign(state.nodes.find(n => n.node_key === 'WF2'), { state: 'SUCCESS', run_state: 'SUCCESS', run_id: 'run-WF2' })
  Object.assign(state.nodes.find(n => n.node_key === 'WF3'), { state: 'ADMITTED', run_state: 'RUNNING', run_id: 'run-WF3' })
  state.nodes.find(n => n.node_key === 'WF4').blockers = [{ parent: 'WF3', state: 'QUEUED', reason: 'Waiting for parent completion' }]
  await page.clock.runFor(2100)
  const rows = region.getByRole('table').getByRole('row')
  await expect(rows.filter({ hasText: 'WF4' })).toContainText('WF3: RUNNING')
  await expect(rows.filter({ hasText: 'WF4' }).getByRole('button', { name: 'View jobs' })).toHaveCount(0)
  state.outcome = 'SUCCESS'
  await rows.filter({ hasText: 'WF2' }).first().getByRole('button', { name: 'View jobs' }).click()
  await expect(page.getByLabel('Task trial run')).toHaveValue('run-WF2')
  expect(state.mutations).toHaveLength(1)
})

test('ambiguous root request retry preserves admission identity', async ({ page }) => {
  const { state, region } = await setup(page, { failStart: true })
  await region.getByRole('button', { name: 'Run WF1', exact: true }).click()
  await expect(region.getByRole('alert')).toHaveText('Admission response unavailable')
  await start(region)
  expect(state.mutations[0].body.requestId).toBe(state.mutations[1].body.requestId)
})

test('failed dispatch preserves successful parent and retry only reevaluates dependencies', async ({ page }) => {
  const { state, region } = await setup(page)
  await start(region)
  Object.assign(state.nodes[0], { state: 'SUCCESS', run_state: 'SUCCESS' })
  state.dispatch = { state: 'ERROR', error: 'Dependency evaluation unavailable: RuntimeError' }
  await page.clock.runFor(2100)
  await expect(region).toContainText('Parent outcomes remain recorded')
  await region.getByRole('button', { name: 'Retry dependency dispatch' }).click()
  await expect(region.getByRole('button', { name: 'Retry dependency dispatch' })).toHaveCount(0)
  expect(state.nodes[0].run_state).toBe('SUCCESS')
  expect(state.mutations.at(-1).path).toMatch(/retry-dispatch$/)
  expect(state.mutations.filter(m => m.path.endsWith('/runs'))).toHaveLength(1)
})

test('unknown outcomes block a second trial and cancellation awaits confirmation', async ({ page }) => {
  const { state, region } = await setup(page)
  await start(region)
  state.cascade.state = 'UNKNOWN'
  Object.assign(state.nodes[0], { state: 'UNKNOWN', run_state: 'UNKNOWN' })
  state.nodes[1].blockers = [{ parent: 'WF1', state: 'UNKNOWN', reason: 'Parent outcome needs review' }]
  await page.clock.runFor(2100)
  await expect(region).toContainText('An execution outcome needs review')
  await expect(region.getByRole('button', { name: 'Run WF1', exact: true })).toBeDisabled()
  await region.getByRole('button', { name: 'Cancel dependency trial' }).click()
  await expect(region.getByRole('button', { name: 'Cancellation requested' })).toBeDisabled()
  expect(state.cascade.state).toBe('UNKNOWN')
})
