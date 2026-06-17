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
  monitor: () => requestJson('/api/monitor'),
  refreshMonitor: () => requestJson('/api/monitor/refresh', { method: 'POST' }),
  runWorkflow: (workflowId) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}/run`, {
    method: 'POST',
    body: JSON.stringify({ triggerSource: 'MANUAL' })
  }),
  history: (limit = 200) => requestJson(`/api/history?limit=${limit}`),
  notifications: () => requestJson('/api/notifications')
}
