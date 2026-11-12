import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

export const activeWorkflowStatuses = new Set(['SUBMITTING', 'INITIATING', 'QUEUED', 'CLAIMED', 'RUNNING', 'UNKNOWN'])

export function applySubmission(workflow, submissions = []) {
  const submission = submissions.find(row => row.workflowId === workflow.WORKFLOW_ID && activeWorkflowStatuses.has(row.status))
  if (!submission || (submission.runId && workflow.run.runId === submission.runId)) return workflow
  const jobIndex = workflow.jobs.findIndex(job => job.JOB_ID === submission.jobId)
  let first = true
  return { ...workflow, canRun: false, runUnavailable: 'Workflow is being prepared.',
    run: { status: 'INITIATING', requestId: submission.requestId, mode: submission.mode, requestedBy: submission.requestedBy, queryId: submission.queryId },
    progress: { total: workflow.jobs.length, succeeded: 0, failed: 0, skipped: 0, percent: 0 },
    jobs: workflow.jobs.map((job, index) => {
      const selected = !job.runOnly && job.JOB_IS_ENABLED !== false &&
        (submission.mode === 'WORKFLOW' || (submission.mode === 'JOB_ONLY' ? index === jobIndex : index >= jobIndex))
      const status = selected ? first ? 'INITIATING' : 'PENDING' : 'SKIPPED'
      if (selected) first = false
      return { ...job, run: { status, message: selected ? '' : 'Not selected for this execution' }, progress: null }
    }) }
}

export default function useOrchestrationMonitor() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [reload, setReload] = useState(0)
  const manual = useRef(false)
  const latest = useRef(null)
  const refresh = useCallback(() => { manual.current = true; setReload(value => value + 1) }, [])
  useEffect(() => {
    let active = true, fetching = false, lastUpdate = 0
    function receive(next) {
      if (!active) return
      if (latest.current?.cacheId && latest.current.cacheId === next.cacheId && latest.current.revision > next.revision) return
      latest.current = next
      setData(next)
      setError(next.error || '')
    }
    async function snapshot(force = false) {
      if (fetching) return
      fetching = true
      setRefreshing(true)
      try { receive(await api.orchestrationMonitor(force)) }
      catch (failure) { if (active) setError(failure.message) }
      finally { fetching = false; if (active) setRefreshing(false) }
    }
    function realtime(event) {
      const message = event.detail
      if (message?.type === 'orchestration_update') { lastUpdate = Date.now(); receive(message.data) }
      if (message?.type === 'connected') snapshot()
    }
    window.addEventListener('kumo:realtime', realtime)
    snapshot(manual.current)
    manual.current = false
    // A disconnected browser may read the shared Python snapshot. This never
    // starts a separate Snowflake refresh for that browser.
    const timer = window.setInterval(() => { if (Date.now() - lastUpdate > 20000) snapshot() }, 15000)
    return () => { active = false; window.clearInterval(timer); window.removeEventListener('kumo:realtime', realtime) }
  }, [reload])
  const acceptSubmission = useCallback(receipt => setData(previous => {
    if (!previous || previous.rows.some(row => row.WORKFLOW_ID === receipt.workflowId && row.run.requestId === receipt.requestId)) return previous
    return { ...previous, submissions: [...(previous.submissions || []).filter(row => row.requestId !== receipt.requestId), receipt] }
  }), [])
  return { data, error, loading: !error && (!data || data.warming), refreshing, refresh, acceptSubmission }
}
