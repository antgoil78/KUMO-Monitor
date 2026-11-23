import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from './StatusBadge.jsx'

export default function TaskEngineDependencies({ onSelectRun }) {
  const [data, setData] = useState(null)
  const [graphId, setGraphId] = useState('')
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [revision, setRevision] = useState(0)
  const pending = useRef(null), busyRef = useRef(false)
  useEffect(() => {
    let alive = true, timer
    async function read() {
      try {
        const result = await api.taskPrototypeGraphs()
        if (!alive) return
        setData(result); setReadError('')
        const graph = result.graphs?.find(g => g.id === graphId) || result.graphs?.[0]
        const id = selected || result.cascades?.find(c => c.graph_id === graph?.id)?.id
        if (id) {
          const value = await api.taskPrototypeCascade(id)
          if (alive) setDetail(value)
        } else setDetail(null)
      } catch (failure) { if (alive) setReadError(failure.message) }
      if (alive) timer = window.setTimeout(read, 2000)
    }
    read()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [graphId, selected, revision])
  const graph = data?.graphs?.find(g => g.id === graphId) || data?.graphs?.[0]
  const cascade = detail?.cascade
  const active = cascade && ['QUEUED', 'RUNNING', 'UNKNOWN'].includes(cascade.state)

  async function action(fn) {
    if (busyRef.current) return
    busyRef.current = true; setBusy(true); setError('')
    try { await fn() }
    catch (failure) { setError(failure.message) }
    finally { busyRef.current = false; setBusy(false); setRevision(value => value + 1) }
  }
  function start() {
    action(async () => {
      if (!pending.current || pending.current.graphId !== graph.id) pending.current = { graphId: graph.id, body: { requestId: `workflow-trial-${window.crypto.randomUUID()}` } }
      const result = await api.startTaskPrototypeGraph(graph.id, pending.current.body)
      pending.current = null; setSelected(result.cascadeId)
    })
  }
  return <section className="task-trial-schedules" aria-label="Workflow dependency trial">
    <h3>Workflow dependency trial</h3>
    <p>Start WF1 here to test the PostgreSQL engine with its copied SQL jobs: <strong>WF1 → (WF2, WF3) → WF4</strong>.</p>
    <p>WF4 waits for both parents. This snapshot is separate from the existing workflow controls below. Only one execution of a trial graph is admitted at a time.</p>
    <p><strong>Dependency dispatcher:</strong> <span>{data?.dispatcher?.state || 'Loading…'}</span></p>
    <div className="task-trial-controls">
      <select aria-label="Dependency trial graph" value={graph?.id || ''} disabled={busy} onChange={e => { setGraphId(e.target.value); setSelected(''); pending.current = null }}>
        {!data?.graphs?.length && <option value="">No dependency trial configured</option>}
        {data?.graphs?.map(value => <option key={value.id} value={value.id}>{value.name} · revision {value.revision}</option>)}
      </select>
      <button type="button" className="button" disabled={busy || !graph?.enabled || !!graph?.active_cascade} onClick={start}>Run WF1</button>
      <select aria-label="Dependency trial execution" value={selected || cascade?.id || ''} onChange={e => setSelected(e.target.value)}>
        {!data?.cascades?.some(c => c.graph_id === graph?.id) && <option value="">No trial executions yet</option>}
        {data?.cascades?.filter(c => c.graph_id === graph?.id).map(c => <option key={c.id} value={c.id}>{c.request_id} · {c.state}</option>)}
      </select>
      {active && <button type="button" className="button" disabled={busy || cascade.cancel_requested} onClick={() => action(() => api.cancelTaskPrototypeCascade(cascade.id))}>{cascade.cancel_requested ? 'Cancellation requested' : 'Cancel dependency trial'}</button>}
      {detail?.dispatch?.state === 'ERROR' && <button type="button" className="button" disabled={busy} onClick={() => action(() => api.retryTaskPrototypeDispatch(cascade.id))}>Retry dependency dispatch</button>}
    </div>
    {(error || readError) && <p className="alert error" role="alert">{error || readError}</p>}
    {cascade && <>
      <p><strong>Trial:</strong> <code>{cascade.id}</code> · <StatusBadge status={cascade.state} /> · frozen graph revision {cascade.graph_revision}</p>
      {detail.dispatch?.error && <p role="status">{detail.dispatch.error}. Parent outcomes remain recorded. Retrying dispatch does not resubmit existing native attempts.</p>}
      {cascade.state === 'UNKNOWN' && <p role="status">An execution outcome needs review. Descendants remain blocked and no automatic native resubmission occurs.</p>}
      <div className="task-trial-table"><table aria-label="Workflow dependency status">
        <thead><tr><th>Workflow</th><th>Execution state</th><th>Required parents</th><th>Waiting / blocked reason</th><th>Execution</th></tr></thead>
        <tbody>{cascade.snapshot.nodes.map(definition => {
          const node = detail.nodes.find(row => row.node_key === definition.key)
          return <tr key={definition.key}>
            <td><strong>{definition.name}</strong><br /><small>{definition.plan.jobs.length} SQL job(s)</small></td>
            <td><StatusBadge status={node?.run_state || node?.state} />{node?.cleanup_pending > 0 && <small> · Cleanup pending</small>}</td>
            <td>{definition.parents.length ? definition.parents.map(rule => `${rule.key}${rule.parentMustBeNewer ? ' (fresh)' : ''}`).join(', ') : 'Root workflow'}</td>
            <td>{node?.blockers?.length ? node.blockers.map((b, index) => {
              const parent = detail.nodes.find(value => value.node_key === b.parent)
              const waiting = b.reason === 'Waiting for parent completion'
              const state = waiting ? parent?.run_state || parent?.state || b.state : b.state
              const reason = waiting && ['SUCCESS', 'FAILED', 'CANCELLED', 'SKIPPED'].includes(state) ? 'Waiting for dependency evaluation' : b.reason
              return <div key={index}>{b.parent && <strong>{b.parent}: </strong>}{state && `${state} · `}{reason}</div>
            }) : node?.state === 'WAITING' ? 'Waiting for dependency evaluation' : '—'}
              {!!Object.keys(node?.parent_runs || {}).length && <details><summary>Parent executions</summary>{Object.entries(node.parent_runs).map(([key, value]) => <div key={key}>{key}: <code>{value}</code></div>)}</details>}</td>
            <td>{node?.run_id ? <button type="button" className="button" onClick={() => onSelectRun(node.run_id)}>View jobs</button> : 'No Snowflake task admitted'}</td>
          </tr>
        })}</tbody>
      </table></div>
    </>}
    <small>Dependency checks and waiting states use PostgreSQL. A completion event releases eligible children; waiting children create no Snowflake tasks. SQL/data-readiness predicates and partial workflow runs are outside this trial.</small>
  </section>
}
