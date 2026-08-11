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

export const fileIngestionApi = {
  overview: (historyDays = 30) =>
    requestJson(`/api/file-ingestion?historyDays=${encodeURIComponent(historyDays)}`),

  raw: (groupName) =>
    requestJson(`/api/file-ingestion/${encodeURIComponent(groupName)}/raw`),

  ready: (groupName) =>
    requestJson(`/api/file-ingestion/${encodeURIComponent(groupName)}/ready`),

  history: (groupName, historyDays = 30) =>
    requestJson(
      `/api/file-ingestion/${encodeURIComponent(groupName)}/history?historyDays=${encodeURIComponent(historyDays)}`
    )
}
