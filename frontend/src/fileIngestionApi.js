async function requestJson(url, options = {}) {
  const { timeoutMs = 45000, ...fetchOptions } = options
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  let response
  try {
    response = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(fetchOptions.headers || {}) },
      credentials: 'same-origin',
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
  } catch {
    throw new Error(`Expected JSON from ${url}, got: ${text.slice(0, 160)}`)
  }

  if (!response.ok) {
    const err = new Error(data?.error || data?.message || `Request failed with ${response.status}`)
    err.status = response.status
    err.data = data
    throw err
  }

  return data
}

const RAW_RELOAD_TIMEOUT_MS = 30 * 60 * 1000

export const fileIngestionApi = {
  subjectAreas: () => requestJson('/api/file-ingestion/reload/subject-areas'),

  overview: () =>
    requestJson('/api/file-ingestion', {
      timeoutMs: 5 * 60 * 1000
    }),

  rawStatus: () =>
    requestJson('/api/file-ingestion/raw-status', {
      timeoutMs: 5 * 60 * 1000
    }),

  raw: (groupName, sourceId) =>
    requestJson(`/api/file-ingestion/${encodeURIComponent(groupName)}/raw${sourceId ? `?sourceId=${encodeURIComponent(sourceId)}` : ''}`, {
      timeoutMs: 5 * 60 * 1000
    }),

  ready: (groupName, sourceId) =>
    requestJson(`/api/file-ingestion/${encodeURIComponent(groupName)}/ready${sourceId ? `?sourceId=${encodeURIComponent(sourceId)}` : ''}`, {
      timeoutMs: 5 * 60 * 1000
    }),

  investigateRowcount: (groupName, fileName, sourceId) => {
    const params = new URLSearchParams({ fileName })
    if (sourceId) params.set('sourceId', sourceId)
    return requestJson(`/api/file-ingestion/${encodeURIComponent(groupName)}/investigate-rowcount?${params.toString()}`, {
      timeoutMs: 5 * 60 * 1000
    })
  },

  history: (groupName, historyDays = 30, sourceId) =>
    requestJson(
      `/api/file-ingestion/${encodeURIComponent(groupName)}/history?historyDays=${encodeURIComponent(historyDays)}${sourceId ? `&sourceId=${encodeURIComponent(sourceId)}` : ''}`,
      { timeoutMs: 5 * 60 * 1000 }
    ),

  reload: (payload) =>
    requestJson('/api/file-ingestion/reload', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: RAW_RELOAD_TIMEOUT_MS
    })
}
