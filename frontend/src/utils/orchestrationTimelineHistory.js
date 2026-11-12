import { executionInterval } from './orchestrationTimeline.js'

const key = run => `${run.workflowId}:${run.runId}`

export function retainedHistoryBoundary(previous, query) {
  if (!previous || previous.pageCount < 2) return null
  const start = Date.parse(query.start), end = Date.parse(query.end)
  // Runs retain the API's descending keyset order. Track the exact last root
  // rather than rounding its timestamp (Snowflake timestamps have microseconds).
  const oldest = previous.runs.filter(run => {
    const interval = executionInterval(run, end)
    return interval && interval.start <= end && interval.end >= start
  }).at(-1)
  return oldest ? key(oldest) : null
}

// Re-read the user's loaded depth atomically. New runs can push previously
// loaded runs onto later pages, so continue to the retained boundary too.
// Every read stays at the API's bounded page size; cancellation stops replays
// as soon as the user changes windows or leaves the timeline.
export async function refreshTimelineHistory(fetchPage, query, previous, force, isCurrent) {
  const boundary = retainedHistoryBoundary(previous, query)
  const depth = previous?.pageCount || 1
  let result, cursor, pageCount = 0
  const runs = new Map(), cursors = new Set()
  do {
    if (!isCurrent()) return null
    const page = await fetchPage({ ...query, ...(cursor ? { cursor } : {}) }, force)
    if (!isCurrent()) return null
    result ||= page
    page.runs.forEach(run => runs.set(key(run), run))
    pageCount++
    cursor = page.nextCursor
    if (!cursor || (pageCount >= depth && (!boundary || runs.has(boundary)))) break
    if (cursors.has(cursor)) throw new Error('History returned a repeated page cursor. Retry the timeline.')
    cursors.add(cursor)
  } while (cursor)
  return { ...result, runs: [...runs.values()], nextCursor: cursor, pageCount }
}
