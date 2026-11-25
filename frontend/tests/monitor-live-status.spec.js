import { test, expect } from './timeline.fixture.js'

test('monitor renders scheduled next-run dates for workflows and their jobs', async ({ page, timeline }) => {
  timeline.monitor.rows[0].scheduler = { state: 'STARTED', nextRun: '2026-11-12T06:00:00Z' }
  await page.goto('/?page=orchestrationMonitor')
  const workflowNextRun = page.locator('.om-parent tr > td').last()
  await expect(workflowNextRun).toContainText('07:00:00')
  await expect(workflowNextRun).toContainText('12 Nov 2026')
  await page.getByRole('button', { name: 'Expand all', exact: true }).click()
  await expect(page.locator('.om-children tr > td').last()).toContainText('07:00:00')
  await expect(page.locator('.om-children tr > td').last()).toContainText('12 Nov 2026 · With workflow')
})

test('monitor shows queue blockers, snapshot age and live status changes', async ({ page, timeline }) => {
  const { monitor } = timeline
  await page.clock.pauseAt(new Date('2026-11-11T12:00:02Z'))
  monitor.cacheId = 'live-status-test'
  monitor.revision = 1
  monitor.rows[0].run = { status: 'QUEUED', requestId: 'current-request', requestedAt: '2026-11-11T11:59:45Z',
    waitingFor: [{ name: 'WF7', status: 'RUNNING' }] }
  monitor.submissions = [{ workflowId: monitor.rows[0].WORKFLOW_ID, requestId: 'current-request', status: 'INITIATING' }]
  await page.goto('/?page=orchestrationMonitor')
  await expect(page.locator('.om-name-detail').first()).toContainText('Waiting for WF7 (RUNNING)')
  await expect(page.locator('.om-parent').getByText('QUEUED', { exact: true })).toBeVisible()
  await expect(page.locator('.om-parent')).toContainText('Waiting to start · 17s')
  await expect(page.locator('.om-parent .om-time').nth(1)).toHaveText('—')
  await expect(page.locator('.summary-updated')).toContainText('13:00:00 · 2s ago')
  await page.clock.runFor(16000)
  await expect(page.locator('.summary-updated')).toContainText('Status delayed')

  const updated = { ...monitor, revision: 2, generatedAt: '2026-11-11T12:00:18Z',
    rows: monitor.rows.map(workflow => ({ ...workflow, run: { requestId: 'current-request', status: 'RUNNING', start: '2026-11-11T12:00:18Z' } })) }
  await page.evaluate(data => window.dispatchEvent(new CustomEvent('kumo:realtime', {
    detail: { type: 'orchestration_update', data }
  })), updated)
  await expect(page.locator('.om-name-detail').first()).not.toContainText('Waiting for')
  await expect(page.locator('.om-parent').getByText('RUNNING', { exact: true })).toBeVisible()
  await expect(page.locator('.om-parent .om-time').nth(1)).toHaveText('0m 00s')
  await expect(page.locator('.summary-updated')).toContainText('13:00:18 · 0s ago')
  await expect(page.locator('.om-status-delayed')).toHaveCount(0)
})
