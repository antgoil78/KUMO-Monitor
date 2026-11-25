import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import StatusBadge from './StatusBadge.jsx'

const blank = { name: '', cron: '* * * * *', timezone: 'Europe/Stockholm', graceSeconds: 60, example: 'success' }
function timestamp(value, zone = 'Europe/Stockholm') {
  if (!value) return '—'
  try { return new Intl.DateTimeFormat('en-GB', { timeZone: zone, dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value)) }
  catch { return '—' }
}

export default function TaskEngineSchedules({ examples, onSelectRun }) {
  const [data, setData] = useState(null)
  const [form, setForm] = useState(blank)
  const [editing, setEditing] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [revision, setRevision] = useState(0)
  const pending = useRef(null), busyRef = useRef(false)
  useEffect(() => {
    let alive = true, timer
    async function read() {
      try {
        const result = await api.taskPrototypeSchedules()
        if (alive) { setData(result); setReadError('') }
      } catch (failure) { if (alive) setReadError(failure.message) }
      if (alive) timer = window.setTimeout(read, 5000)
    }
    read()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [revision])

  function change(field, value) {
    pending.current = null
    setForm(current => ({ ...current, [field]: value }))
  }
  async function action(fn) {
    if (busyRef.current) return
    busyRef.current = true; setBusy(true); setError('')
    try { await fn(); setRevision(value => value + 1) }
    catch (failure) { setError(failure.message); setRevision(value => value + 1) }
    finally { busyRef.current = false; setBusy(false) }
  }
  function save(event) {
    event.preventDefault()
    action(async () => {
      const values = { name: form.name, cron: form.cron, timezone: form.timezone, graceSeconds: Number(form.graceSeconds) }
      if (editing) {
        await api.updateTaskPrototypeSchedule(editing.id, { ...values, expectedRevision: editing.revision })
      } else {
        const example = examples[form.example]
        pending.current ||= { ...values, requestId: `schedule-trial-${window.crypto.randomUUID()}`, enabled: false,
          plan: { workflowKey: `trial-${form.example}`, mode: example.mode, jobs: example.jobs } }
        await api.createTaskPrototypeSchedule(pending.current)
      }
      pending.current = null; setEditing(null); setForm(blank)
    })
  }
  function edit(schedule) {
    pending.current = null; setEditing(schedule); setError('')
    setForm({ name: schedule.name, cron: schedule.cron, timezone: schedule.timezone, graceSeconds: schedule.grace_seconds, example: 'success' })
  }
  return <section className="task-trial-schedules" aria-label="Trial schedules">
    <h3>Trial schedules</h3>
    <p className="task-trial-scheduler-health"><strong>Scheduler:</strong> <span>{data?.scheduler?.state || 'Loading…'}</span>
      {' · '}Last heartbeat: {timestamp(data?.scheduler?.lastSeen)} Europe/Stockholm</p>
    <p>Schedules run without a logged-in user. Create a paused schedule, then enable it when ready. Pausing prevents future admissions; an admitted run continues.</p>
    <form className="task-trial-schedule-form" onSubmit={save} aria-label={editing ? 'Edit trial schedule' : 'Create trial schedule'}>
      <label>Name<input aria-label="Schedule name" required maxLength={80} value={form.name} disabled={busy} onChange={e => change('name', e.target.value)} /></label>
      {!editing && <label>Example<select aria-label="Scheduled example" value={form.example} disabled={busy} onChange={e => change('example', e.target.value)}>
        {Object.entries(examples).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
      </select></label>}
      <label>Cron<input aria-label="Schedule cron" required maxLength={120} value={form.cron} disabled={busy} onChange={e => change('cron', e.target.value)} /></label>
      <label>Timezone<input aria-label="Schedule timezone" required value={form.timezone} disabled={busy} onChange={e => change('timezone', e.target.value)} /></label>
      <label>Grace (seconds)<input aria-label="Schedule grace seconds" type="number" min="0" max="3600" required value={form.graceSeconds} disabled={busy} onChange={e => change('graceSeconds', e.target.value)} /></label>
      <button className="button" type="submit" disabled={busy}>{editing ? 'Save schedule' : 'Create paused schedule'}</button>
      {editing && <button className="button" type="button" disabled={busy} onClick={() => { setEditing(null); setForm(blank); pending.current = null }}>Cancel edit</button>}
    </form>
    <small>Five numeric cron fields. Missed occurrences are coalesced; at most the latest due occurrence runs within its grace period. Overlapping runs of the same trial workflow are skipped. Missing daylight-saving times are skipped; repeated times run at their first occurrence.</small>
    {(error || readError) && <p className="alert error" role="alert">{error || readError}</p>}
    <div className="task-trial-table"><table aria-label="Trial schedule list">
      <thead><tr><th>Name / schedule</th><th>Schedule state</th><th>Next planned run</th><th>Last occurrence</th><th>Execution</th><th>Actions</th></tr></thead>
      <tbody>{(data?.schedules || []).map(schedule => <tr key={schedule.id}>
        <td><strong>{schedule.name}</strong><br /><code>{schedule.cron}</code><br /><small>{schedule.timezone}</small></td>
        <td>{schedule.state}{schedule.validation_error && <p>{schedule.validation_error}</p>}</td>
        <td>{timestamp(schedule.next_fire_at, schedule.timezone)}</td>
        <td>{schedule.last_occurrence ? <>{schedule.last_occurrence.state}<br /><small>{timestamp(schedule.last_occurrence.scheduledFor, schedule.timezone)}</small></> : '—'}</td>
        <td>{schedule.active_run ? <StatusBadge status={schedule.active_run.state} /> : schedule.last_occurrence?.runState ? <StatusBadge status={schedule.last_occurrence.runState} /> : '—'}</td>
        <td><div className="task-trial-controls">
          <button className="button" type="button" disabled={busy || !!schedule.validation_error} onClick={() => action(() => api.updateTaskPrototypeSchedule(schedule.id, { expectedRevision: schedule.revision, enabled: !schedule.enabled }))}>{schedule.enabled ? 'Pause' : 'Enable'}</button>
          <button className="button" type="button" disabled={busy} onClick={() => edit(schedule)}>Edit</button>
          {(schedule.active_run?.id || schedule.last_occurrence?.runId) && <button className="button" type="button" onClick={() => onSelectRun(schedule.active_run?.id || schedule.last_occurrence.runId)}>View run</button>}
        </div></td>
      </tr>)}</tbody>
    </table></div>
    {!data?.schedules?.length && <p>No trial schedules yet.</p>}
    {!!data?.occurrences?.length && <div className="task-trial-table"><table aria-label="Schedule occurrences">
      <thead><tr><th>Schedule</th><th>Planned time (Europe/Stockholm)</th><th>Occurrence</th><th>Execution</th><th>Details</th></tr></thead>
      <tbody>{data.occurrences.map(item => <tr key={item.id}>
        <td>{data.schedules.find(s => s.id === item.schedule_id)?.name || item.schedule_id}</td>
        <td>{timestamp(item.scheduled_for)}{item.window_end && <><br /><small>Until {timestamp(item.window_end)} (exclusive)</small></>}</td>
        <td>{item.state}</td><td>{item.run_state ? <StatusBadge status={item.run_state} /> : '—'}</td>
        <td>{item.reason || 'Admitted'}{item.run_id && <> <button className="button" type="button" onClick={() => onSelectRun(item.run_id)}>View run</button></>}</td>
      </tr>)}</tbody>
    </table></div>}
    <small>Schedule state and scheduler health refresh from PostgreSQL every five seconds while this view is mounted.</small>
  </section>
}
