import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from './StatusBadge.jsx'
import TaskEngineSchedules from './TaskEngineSchedules.jsx'
import TaskEngineDependencies from './TaskEngineDependencies.jsx'
import './TaskEngineTrial.css'

const examples = {
  success: { label: 'Success', mode: 'SEQUENTIAL', jobs: [{ key: 'first', sql: 'SELECT 1' }, { key: 'second', sql: 'SELECT 2' }] },
  failure: { label: 'Failure and dependent skip', mode: 'SEQUENTIAL', jobs: [{ key: 'fails', sql: 'SELECT 1/0' }, { key: 'skipped', sql: 'SELECT 2' }] },
  timeout: { label: 'Timeout', mode: 'SEQUENTIAL', jobs: [{ key: 'timeout', sql: "SELECT SYSTEM$WAIT(20, 'SECONDS')", timeoutSeconds: 5 }] },
  parallel: { label: 'Parallel jobs', mode: 'PARALLEL', jobs: [{ key: 'one', sql: "SELECT SYSTEM$WAIT(30, 'SECONDS')" }, { key: 'two', sql: "SELECT SYSTEM$WAIT(30, 'SECONDS')" }] },
  cancel: { label: 'Long job to cancel', mode: 'SEQUENTIAL', jobs: [{ key: 'long', sql: "SELECT SYSTEM$WAIT(90, 'SECONDS')", timeoutSeconds: 120 }] }
}

export default function TaskEngineTrial() {
  const [enabled, setEnabled] = useState(false)
  const [status, setStatus] = useState(null)
  const [detail, setDetail] = useState(null)
  const [selected, setSelected] = useState('')
  const [example, setExample] = useState('success')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [revision, setRevision] = useState(0)
  const pending = useRef(null)
  const busyRef = useRef(false)
  useEffect(() => {
    let alive = true
    api.health().then(value => { if (alive) setEnabled(!!value.taskPrototypeEnabled) }).catch(() => {})
    return () => { alive = false }
  }, [])
  useEffect(() => {
    if (!enabled) return undefined
    let alive = true, timer
    async function read() {
      try {
        const value = await api.taskPrototypeStatus()
        if (!alive) return
        setStatus(value); setReadError('')
        const id = selected || value.runs[0]?.id
        if (id) {
          const run = await api.taskPrototypeRun(id)
          if (alive) setDetail(run)
        }
        if (alive && (Object.entries(value.counts).some(([state, count]) => count > 0 && ['CREATING', 'SUBMITTING', 'SUBMITTED', 'SCHEDULED', 'RUNNING'].includes(state)) || value.runs.some(run => run.state === 'QUEUED') || value.cleanupRetrying > 0)) timer = window.setTimeout(read, 2000)
      } catch (failure) { if (alive) setReadError(failure.message) }
    }
    read()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [enabled, selected, busy, revision])

  async function submit() {
    if (busyRef.current) return
    busyRef.current = true; setBusy(true); setError('')
    const value = examples[example]
    pending.current ||= { requestId: `trial-${window.crypto.randomUUID()}`, workflowKey: `trial-${example}`, mode: value.mode, jobs: value.jobs }
    try {
      const result = await api.submitTaskPrototype(pending.current)
      pending.current = null; setSelected(result.runId)
    } catch (failure) { setError(failure.message) }
    finally { busyRef.current = false; setBusy(false) }
  }

  async function cancel() {
    if (busyRef.current || !detail) return
    busyRef.current = true; setBusy(true)
    try { setDetail(await api.cancelTaskPrototype(detail.run.id)); setError('') }
    catch (failure) { setError(failure.message) }
    finally { busyRef.current = false; setBusy(false) }
  }

  if (!enabled) return null
  const current = detail?.run
  return <details className="task-trial" open>
    <summary>DEV task engine trial</summary>
    <p>Disposable SQL examples use PostgreSQL state and native Snowflake tasks. Existing workflows below keep their current engine.</p>
    <div className="task-trial-controls">
      <select aria-label="Task trial example" value={example} disabled={busy} onChange={event => { setExample(event.target.value); pending.current = null }}>
        {Object.entries(examples).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
      </select>
      <button className="button primary" type="button" onClick={submit} disabled={busy}>{busy ? 'Working…' : 'Run example'}</button>
      <button className="button" type="button" onClick={() => setRevision(value => value + 1)} disabled={busy}>Refresh trials</button>
      <select aria-label="Task trial run" value={selected || current?.id || ''} onChange={event => setSelected(event.target.value)}>
        {!status?.runs?.length && !selected && <option value="">No trial runs yet</option>}
        {selected && !status?.runs?.some(run => run.id === selected) && <option value={selected}>Selected run · {selected}</option>}
        {status?.runs?.map(run => <option key={run.id} value={run.id}>{run.request_id} · {run.state}</option>)}
      </select>
      {current && ['QUEUED', 'RUNNING'].includes(current.state) && <button className="button" type="button" disabled={busy || current.cancel_requested} onClick={cancel}>{current.cancel_requested ? 'Cancellation requested' : 'Cancel run'}</button>}
    </div>
    {(error || readError) && <div className="alert error" role="alert">{error || readError}</div>}
    {current && <><p><strong>Run:</strong> <code>{current.id}</code> · <StatusBadge status={current.state} /></p>
      <div className="task-trial-table"><table><thead><tr><th>Job</th><th>Status</th><th>Task cleanup</th><th>Details</th></tr></thead>
        <tbody>{detail.jobs.map(job => <tr key={job.id}><td>{job.job_key}</td><td><StatusBadge status={job.state} /></td><td>{job.cleanup_state}</td><td>{job.error || job.cleanup_error || job.query_id || '—'}</td></tr>)}</tbody></table></div>
      {current.state === 'UNKNOWN' && <p role="status">Execution needs review. The worker has stopped automatic recovery and will not resubmit it.</p>}
    </>}
    <small>Status reads use PostgreSQL. Automatic refresh stops once runs and cleanup finish.</small>
    <TaskEngineDependencies onSelectRun={id => { setSelected(id); setRevision(value => value + 1) }} />
    <TaskEngineSchedules examples={examples} onSelectRun={id => { setSelected(id); setRevision(value => value + 1) }} />
  </details>
}
