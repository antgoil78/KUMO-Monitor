async function requestJson(url, options = {}) {
  const { timeoutMs = 45000, ...fetchOptions } = options
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  let response
  try {
    response = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(fetchOptions.headers || {}) },
      signal: controller.signal,
      ...fetchOptions
    })
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s: ${url}`)
    }
    throw err
  } finally {
    window.clearTimeout(timer)
  }

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

function isCsrfMismatch(error) {
  return /csrf/i.test(String(error?.message || '')) && /mismatch|missing|token/i.test(String(error?.message || ''))
}

export const api = {
  health: () => requestJson('/api/health'),
  session: () => requestJson('/api/session'),
  activeUsers: () => requestJson('/api/users/active'),
  snowflakePing: () => requestJson('/api/snowflake/ping'),
  monitor: () => requestJson('/api/monitor'),
  refreshMonitor: () => requestJson('/api/monitor/refresh', { method: 'POST' }),
  workflowRunLocks: () => requestJson('/api/workflow-run-locks', { timeoutMs: 12000 }),
  realtimeState: () => requestJson('/api/realtime/state', { timeoutMs: 5000 }),
  runWorkflow: async (workflowId, workflowName = '') => {
    const encodedId = encodeURIComponent(workflowId)
    try {
      return await requestJson(`/api/workflows/${encodedId}/run`, {
        method: 'POST',
        body: JSON.stringify({ triggerSource: 'MANUAL', workflowName })
      })
    } catch (err) {
      if (!isCsrfMismatch(err)) throw err
      const params = new URLSearchParams({ triggerSource: 'MANUAL', workflowName })
      return requestJson(`/api/workflows/${encodedId}/run-fallback?${params.toString()}`)
    }
  },
  workflowDetail: (workflowId, options = {}) => requestJson(`/api/workflows/${encodeURIComponent(workflowId)}`, options),
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


export function createKumoEventSource(onEvent, onError) {
  if (typeof window === 'undefined' || typeof window.EventSource === 'undefined') {
    return null
  }
  const source = new window.EventSource('/api/events')
  const eventTypes = ['connected', 'monitor_update', 'workflow_run_requested', 'workflow_run_queued', 'workflow_run_status', 'workflow_run_failed']

  function handle(event) {
    try {
      const payload = event.data ? JSON.parse(event.data) : null
      onEvent?.(payload || { type: event.type, data: {} })
    } catch (err) {
      onError?.(err)
    }
  }

  eventTypes.forEach(type => source.addEventListener(type, handle))
  source.onerror = () => onError?.(new Error('Realtime event stream disconnected'))
  return source
}
