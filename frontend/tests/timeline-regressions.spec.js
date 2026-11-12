import { test, expect } from './timeline.fixture.js'

const openTimeline = async page => {
  await page.goto('/?page=orchestrationMonitor&view=timeline')
  await expect(page.locator('.ot-window')).toContainText('displayed')
}

test('live refresh retains paginated runs even when new runs push them onto later pages', async ({ page, timeline }) => {
  const { older, state, makeRun, requests } = timeline
  await openTimeline(page)
  await page.getByRole('button', { name: 'Load more runs', exact: true }).click()
  const olderBar = page.locator(`[data-execution-id="${older.executionId}"]`)
  await expect(olderBar).toHaveCount(1)
  state.historyRows.unshift(...[1, 2, 3].map(i => makeRun(`new-${i}`, `2026-11-11T11:59:1${i}Z`, `2026-11-11T11:59:2${i}Z`, 'SUCCESS')))
  const before = requests.filter(req => req.path.endsWith('/history')).length
  await page.clock.fastForward(16000)
  await expect.poll(() => requests.filter(req => req.path.endsWith('/history')).length).toBeGreaterThan(before + 2)
  await expect(page.locator('.ot-window')).toContainText('7 history runs loaded')
  await expect(olderBar).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Load more runs', exact: true })).toHaveCount(0)
  for (const run of state.historyRows) await expect(page.locator(`[data-execution-id="${run.executionId}"]`)).toHaveCount(1)
})

test('older history survives log and DAG round trips with zoom and scroll restored', async ({ page, timeline }) => {
  await openTimeline(page)
  await page.getByRole('button', { name: 'Live', exact: true }).click()
  await expect(page.locator('.ot-window')).toContainText('displayed')
  await page.getByRole('button', { name: 'Load more runs', exact: true }).click()
  await page.getByLabel('Timeline zoom', { exact: true }).selectOption('43200000')
  const olderBar = () => page.locator(`[data-execution-id="${timeline.older.executionId}"]`)
  for (const action of ['View log', 'Show DAG run']) {
    await olderBar().click()
    const x = await page.locator('.ot-viewport').evaluate(node => node.scrollLeft)
    await page.getByRole('dialog', { name: 'Run actions', exact: true }).getByRole('button', { name: action, exact: true }).click()
    await expect(page.locator(action === 'View log' ? '.execution-log-page' : '.dag-page')).toBeVisible()
    const request = timeline.requests.filter(req => req.path.endsWith(action === 'View log' ? '/log' : '/dag')).at(-1)
    expect(request.query.runId).toBe(timeline.older.runId)
    await page.getByRole('button', { name: action === 'View log' ? '← Back' : '← Back to monitor', exact: true }).click()
    await expect(page.locator('.ot-window')).toContainText('4 history runs loaded')
    await expect(olderBar()).toHaveCount(1)
    await expect(olderBar()).toHaveClass(/ot-selected/)
    await expect(page.getByLabel('Timeline zoom', { exact: true })).toHaveValue('43200000')
    await expect.poll(async () => Math.abs(await page.locator('.ot-viewport').evaluate(node => node.scrollLeft) - x)).toBeLessThan(2)
  }
})

test('a slow paginated refresh completes across polling ticks', async ({ page, timeline }) => {
  await openTimeline(page)
  await page.getByRole('button', { name: 'Load more runs', exact: true }).click()
  await expect(page.locator('.ot-window')).toContainText('4 history runs loaded')
  let release
  const gate = new Promise(resolve => { release = resolve })
  timeline.state.beforeHistory = async url => {
    if (url.searchParams.has('cursor')) await gate
  }
  const reads = () => timeline.requests.filter(req => req.path.endsWith('/history')).length
  const before = reads()
  try {
    await page.clock.fastForward(16000)
    await expect.poll(reads).toBe(before + 2)
    await expect(page.locator('.ot-window')).toContainText('Loading execution history')
    await page.clock.fastForward(16000)
    // No new head query starts while the cursor page is still in flight.
    expect(reads()).toBe(before + 2)
  } finally {
    timeline.state.beforeHistory = null
    release()
  }
  await expect(page.locator('.ot-window')).toContainText('4 history runs loaded')
  await expect(page.locator(`[data-execution-id="${timeline.older.executionId}"]`)).toHaveCount(1)
})

test('failed refresh keeps loaded history, retries it, and a new range clears it', async ({ page, timeline }) => {
  await openTimeline(page)
  await page.getByRole('button', { name: 'Load more runs', exact: true }).click()
  await expect(page.locator('.ot-window')).toContainText('4 history runs loaded')
  timeline.state.failHistory = true
  await page.clock.fastForward(16000)
  await expect(page.getByRole('alert')).toContainText('History temporarily unavailable')
  await expect(page.locator(`[data-execution-id="${timeline.older.executionId}"]`)).toHaveCount(1)
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.locator('.ot-window')).toContainText('4 history runs loaded')
  await page.getByLabel('Timeline range', { exact: true }).selectOption('86400000')
  await expect(page.locator('.ot-window')).toContainText('3 history runs loaded')
  await expect(page.locator(`[data-execution-id="${timeline.older.executionId}"]`)).toHaveCount(0)
})

test('dependency drafts require confirmation before sidebar navigation and guard reloads', async ({ page, timeline }) => {
  await page.goto('/?page=orchestrationDependencies')
  const allow = page.getByLabel('PYMT LOAD allow', { exact: true })
  await allow.selectOption('false')
  await expect(page.getByText('Unsaved changes', { exact: true })).toBeVisible()
  const beforeUnloadPrevented = () => page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  })
  expect(await beforeUnloadPrevented()).toBe(true)
  const cancelled = page.waitForEvent('dialog').then(async dialog => {
    expect(dialog.message()).toContain('unsaved dependency changes')
    await dialog.dismiss()
  })
  await page.getByRole('button', { name: 'Monitor', exact: true }).click()
  await cancelled
  await expect(allow).toHaveValue('false')
  await expect(page.getByText('Unsaved changes', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Discard changes', exact: true }).click()
  expect(await beforeUnloadPrevented()).toBe(false)
  await allow.selectOption('false')
  const accepted = page.waitForEvent('dialog').then(dialog => dialog.accept())
  await page.getByRole('button', { name: 'Monitor', exact: true }).click()
  await accepted
  await expect(page.getByRole('heading', { name: 'Workflow Monitor', exact: true })).toBeVisible()
  expect(await beforeUnloadPrevented()).toBe(false)
})
