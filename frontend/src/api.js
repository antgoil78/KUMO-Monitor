async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  })

  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch (err) {
    throw new Error(`Expected JSON from ${url}, got: ${text.slice(0, 160)}`)
  }

  if (!response.ok) {
    throw new Error(data?.error || data?.message || `Request failed with ${response.status}`)
  }
  return data
}

export const api = {
  health: () => requestJson('/api/health'),
  session: () => requestJson('/api/session'),
  activeUsers: () => requestJson('/api/users/active'),
  snowflakePing: () => requestJson('/api/snowflake/ping'),
  monitor: () => requestJson('/api/monitor'),
  refreshMonitor: () => requestJson('/api/monitor/refresh', { method: 'POST' }),
  runWorkflow: (workflowId) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/run`, {
    method: 'POST',
    body: JSON.stringify({ triggerSource: 'MANUAL' })
  }),
  workflowDetail: (workflowId) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}`),
  updateWorkflow: (workflowId, payload) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload)
  }),
  cloneWorkflow: (workflowId) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/clone`, { method: 'POST' }),
  deleteWorkflow: (workflowId) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}`, { method: 'DELETE' }),
  setWorkflowEnabled: (workflowId, enabled) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/workflow-enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled })
  }),
  setScheduleEnabled: (workflowId, enabled) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/schedule-enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled })
  }),
  workflowHistory: (workflowId, limit = 100) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/history?limit=${limit}`),
  workflowDag: (workflowId) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/dag`),
  history: (limit = 200) => requestJson(`/api/history?limit=${limit}`),
  notifications: () => requestJson('/api/notifications')
}
