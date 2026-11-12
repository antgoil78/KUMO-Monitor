export const MIN_SPAN = 1000
export const MAX_SPAN = 30 * 86400000
export const activeTimelineStatuses = new Set(['RUNNING', 'CLAIMED', 'QUEUED', 'INITIATING', 'PENDING'])
const stockholm = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Stockholm', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })

export function stockholmInput(ms) {
  return stockholm.format(new Date(ms)).replace(' ', 'T')
}

export function parseStockholmInput(value) {
  // Try both Stockholm offsets. Reject nonexistent times at the spring DST jump;
  // choose the first occurrence when an autumn clock time happens twice.
  const nominal = Date.parse(`${value}Z`)
  if (!Number.isFinite(nominal)) return null
  const normalized = value.length === 16 ? `${value}:00` : value
  for (const offset of [120, 60]) {
    const candidate = nominal - offset * 60000
    if (stockholmInput(candidate) === normalized) return candidate
  }
  return null
}

export function executionInterval(run, now) {
  const started = Date.parse(run.start || run.requestedAt || '')
  if (!Number.isFinite(started)) return null
  const ended = Date.parse(run.end || '')
  const end = Number.isFinite(ended) ? ended : activeTimelineStatuses.has(run.status) ? now : started
  return { start: started, end: Math.max(started, end) }
}

export function barGeometry(interval, start, end) {
  if (!interval || interval.end < start || interval.start > end) return null
  const left = (Math.max(start, interval.start) - start) / (end - start) * 100
  const width = (Math.min(end, interval.end) - Math.max(start, interval.start)) / (end - start) * 100
  return { left, width, clippedStart: interval.start < start, clippedEnd: interval.end > end }
}

export function timelineLanes(runs, start, end, now, width) {
  const lanes = []
  const items = runs.map(run => ({ run, interval: executionInterval(run, now) }))
    .map(item => ({ ...item, geometry: barGeometry(item.interval, start, end) }))
    .filter(item => item.geometry).sort((a, b) => a.interval.start - b.interval.start)
  for (const item of items) {
    const x = item.geometry.left / 100 * width
    let lane = lanes.findIndex(right => right + 3 <= x)
    if (lane < 0) lane = lanes.length
    lanes[lane] = x + Math.max(5, item.geometry.width / 100 * width)
    item.lane = lane
  }
  return { items, count: Math.max(1, lanes.length) }
}

export function executionKey(run) {
  return `${run.workflowId}:${run.executionId}`
}

export function executionParents(runs) {
  const parents = new Map()
  for (const root of runs) {
    if (root.parentExecutionId) parents.set(executionKey(root), root.parentExecutionId)
    // Older cached snapshots lack parent IDs. Their saved execution order can
    // connect selected/started jobs within this exact run, never another run.
    const jobs = root.jobs.filter(job => job.selected !== false && (job.parentExecutionId || job.selected === true || job.start || activeTimelineStatuses.has(job.status)))
      .sort((a, b) => (a.position ?? Infinity) - (b.position ?? Infinity))
    let previous = root.executionId
    for (const job of jobs) {
      const parent = job.parentExecutionId === undefined && Number.isFinite(job.position) ? previous : job.parentExecutionId
      if (parent) parents.set(executionKey({ ...job, workflowId: root.workflowId }), parent)
      previous = job.executionId
    }
  }
  return parents
}

export function connectedExecutions(runs, selection) {
  if (!selection) return new Set()
  const parents = executionParents(runs)
  const nodes = runs.flatMap(root => [root, ...root.jobs.map(job => ({ ...job, workflowId: root.workflowId }))])
  const byId = new Map(nodes.map(run => [run.executionId, executionKey(run)]))
  const neighbors = new Map()
  for (const [child, parentId] of parents) {
    const parent = byId.get(parentId)
    if (!parent) continue
    if (!neighbors.has(parent)) neighbors.set(parent, [])
    if (!neighbors.has(child)) neighbors.set(child, [])
    neighbors.get(parent).push(child); neighbors.get(child).push(parent)
  }
  const connected = new Set(), pending = [executionKey(selection)]
  while (pending.length) {
    const key = pending.pop()
    if (connected.has(key)) continue
    connected.add(key); pending.push(...(neighbors.get(key) || []))
  }
  return connected
}

export function timelineLayout(rows, runs, start, end, now, width, compact = false) {
  const laneStep = compact ? 23 : 28, barHeight = compact ? 18 : 23
  const locations = new Map(), parents = executionParents(runs)
  let height = 0
  const laidOut = rows.map(row => {
    const lanes = timelineLanes(row.runs, start, end, now, width)
    const rowHeight = Math.max(compact ? 40 : 54, lanes.count * laneStep + 12)
    for (const item of lanes.items) {
      const left = Math.min(width - 5, item.geometry.left / 100 * width)
      const right = Math.min(width, left + Math.max(5, item.geometry.width / 100 * width))
      locations.set(item.run.executionId, { run: item.run, left, right, y: height + 6 + item.lane * laneStep + barHeight / 2, workflow: !row.job })
    }
    const result = { ...row, lanes, height: rowHeight, top: height }
    height += rowHeight
    return result
  })
  const connections = []
  for (const child of locations.values()) {
    const parent = locations.get(parents.get(executionKey(child.run)))
    if (!parent || parent === child) continue
    const fromX = parent.workflow && parent.run.runId === child.run.runId ? parent.left : parent.right
    const toX = child.left
    const fromY = parent.y + barHeight / 2, toY = child.y - barHeight / 2
    // Use the gap between rows as a bend; bar endpoints stay accurate when
    // clipped, zoomed, stacked in lanes, or shown with a minimum pixel width.
    const bendY = (fromY + toY) / 2
    const path = `M ${fromX} ${fromY} V ${bendY} H ${toX} V ${toY}`
    connections.push({ id: `${parent.run.executionId}:${child.run.executionId}`, parent: parent.run, child: child.run, fromX, toX, fromY, toY, path, waiting: !child.run.start })
  }
  return { rows: laidOut, height, connections }
}

const tickSteps = [100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 900000, 1800000, 3600000, 7200000, 10800000, 21600000, 43200000, 86400000, 172800000, 604800000]
export function timelineTicks(start, end, width) {
  const target = (end - start) / Math.max(2, width / 130)
  const step = tickSteps.find(value => value >= target) || tickSteps.at(-1)
  const ticks = []
  for (let at = Math.ceil(start / step) * step; at < end; at += step) ticks.push({ at, left: (at - start) / (end - start) * 100 })
  return { ticks, step }
}

export function preciseDuration(run, now) {
  if (!run.start) return activeTimelineStatuses.has(run.status) ? 'Waiting to start' : 'Not started'
  const interval = executionInterval(run, now)
  if (!interval || (!run.end && !activeTimelineStatuses.has(run.status))) return 'Duration unavailable'
  const ms = interval.end - interval.start
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(3)} s`
  const seconds = Math.floor(ms / 1000)
  return `${seconds >= 3600 ? `${Math.floor(seconds / 3600)}h ` : ''}${Math.floor(seconds / 60) % 60}m ${seconds % 60}s`
}
