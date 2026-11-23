import { test, expect } from '@playwright/test'

test('edit current workflow notification rules, retain a failed save, and reload saved settings', async ({ page }) => {
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  const workflowId = '00000000-0000-0000-0000-000000000001'
  const state = { reject: true, submissions: [] }
  let workflow = { WORKFLOW_ID: workflowId, WORKFLOW_NAME: 'ADTX LOAD', WORKFLOW_GROUP: 'ADTX',
    ON_FAIL_EMAIL: true, ON_SUCCESS_EMAIL: false, FAIL_GROUP: 'KUMO_DEV', SUCCESS_GROUP: '', EMAIL_INTEGRATION: 'MY_EMAIL_INT', ENVIRONMENT: 'DEV' }
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let body = { ok: true }
    if (path === '/api/health') body = { ok: true, environment: 'DEV', legacyEngineRetired: true }
    if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
    if (path === '/api/notification-admin') body = { ok: true, groups: [{ GROUP_NAME: 'KUMO_DEV', RECIPIENTS: 'ops@example.test' }], workflows: [workflow] }
    if (path === `/api/notification-workflows/${workflowId}`) {
      const payload = route.request().postDataJSON()
      state.submissions.push(payload)
      if (state.reject) return route.fulfill({ status: 400, json: { error: 'Choose an email group with recipients' } })
      workflow = { ...workflow, ON_FAIL_EMAIL: payload.onFailEmail, ON_SUCCESS_EMAIL: payload.onSuccessEmail,
        FAIL_GROUP: payload.failGroup, SUCCESS_GROUP: payload.successGroup, EMAIL_INTEGRATION: payload.emailIntegration }
    }
    await route.fulfill({ json: body })
  })
  await page.goto('/?page=notifications')
  await page.getByRole('tab', { name: 'Workflow Config' }).click()
  await expect(page.locator('.notification-config-table')).toContainText('ADTX LOAD')
  await page.getByRole('button', { name: 'Edit notifications for ADTX LOAD' }).click()
  const dialog = page.getByRole('dialog', { name: 'Notifications: ADTX LOAD' })
  await expect(dialog.getByLabel('Failure emails', { exact: true })).toBeChecked()
  await dialog.getByLabel('Success emails', { exact: true }).check()
  await dialog.getByLabel('Success recipient group').selectOption('KUMO_DEV')
  await dialog.getByRole('button', { name: 'Save notifications' }).click()
  await expect(dialog.getByRole('alert')).toHaveText('Choose an email group with recipients')
  await expect(dialog.getByLabel('Success emails', { exact: true })).toBeChecked()
  state.reject = false
  await dialog.getByRole('button', { name: 'Save notifications' }).click()
  await expect(dialog).not.toBeVisible()
  expect(state.submissions.at(-1)).toEqual({ onFailEmail: true, onSuccessEmail: true, failGroup: 'KUMO_DEV', successGroup: 'KUMO_DEV', emailIntegration: 'MY_EMAIL_INT' })
  await page.reload()
  await page.getByRole('tab', { name: 'Workflow Config' }).click()
  await page.getByRole('button', { name: 'Edit notifications for ADTX LOAD' }).click()
  await expect(dialog.getByLabel('Success emails', { exact: true })).toBeChecked()
  await expect(dialog.getByLabel('Success recipient group')).toHaveValue('KUMO_DEV')
})
