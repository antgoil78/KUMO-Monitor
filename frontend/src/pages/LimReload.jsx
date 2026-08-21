import { useEffect, useMemo, useState } from 'react'
import { fileIngestionApi } from '../fileIngestionApi.js'
import './LimReload.css'

const modes = [
  { value: 'NORMAL', label: 'New load', description: 'Load matching files that Snowflake has not already loaded.' },
  { value: 'PARTIAL_RELOAD', label: 'Partial reload', description: 'Clean and force reload files in a delivery end-date range.' },
  { value: 'FULL_RELOAD', label: 'Full reload', description: 'Truncate the subject-area RAW table and reload all staged files.' }
]

const resultLabels = {
  STATUS: 'Status', LOAD_MODE: 'Load mode', LIM_FORMAT: 'LIM format', FROM_DLVY_END_DATE: 'From date',
  TO_DLVY_END_DATE: 'To date', FILES_SELECTED: 'Files selected', RAW_ROWS_DELETED: 'RAW rows deleted',
  SET_READY_LOG_DELETED: 'Ready log deleted',
  TMP_SET_READY_LOG_DELETED: 'Temporary log deleted', PKG_CONTROL_ROWS_RESET: 'Package controls reset',
  COPY_COMMANDS_EXECUTED: 'Copy commands'
}

export default function LimReload() {
  const [form, setForm] = useState({ limFormat: '', mode: 'PARTIAL_RELOAD', fromDate: '', toDate: '', resetPackageCheck: true, setReadyToLoad: false, confirmed: false })
  const [running, setRunning] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [subjectAreas, setSubjectAreas] = useState([])
  const [subjectsLoading, setSubjectsLoading] = useState(true)
  const [subjectsError, setSubjectsError] = useState('')
  const isFull = form.mode === 'FULL_RELOAD'
  const isReload = form.mode !== 'NORMAL'
  const hasDateRange = Boolean(form.fromDate && form.toDate)
  const usesDateRange = !isFull && hasDateRange
  const effectiveFullReload = isReload && !usesDateRange
  const selectedMode = modes.find(mode => mode.value === form.mode)

  useEffect(() => {
    let cancelled = false
    fileIngestionApi.subjectAreas().then(response => {
      if (cancelled) return
      const values = response.subjectAreas || []
      setSubjectAreas(values)
      setForm(previous => previous.limFormat || !values.length ? previous : { ...previous, limFormat: values[0] })
    }).catch(err => {
      if (!cancelled) setSubjectsError(err.message)
    }).finally(() => {
      if (!cancelled) setSubjectsLoading(false)
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!running) {
      setElapsedSeconds(0)
      return undefined
    }
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [running])

  const validation = useMemo(() => {
    if (!form.limFormat) return subjectsLoading ? 'Loading subject areas…' : 'Select a LIM subject area.'
    if (!isFull && Boolean(form.fromDate) !== Boolean(form.toDate)) return 'Select both delivery end dates or leave both blank.'
    if (!isFull && hasDateRange && form.fromDate > form.toDate) return 'The from date must be on or before the to date.'
    if (isReload && !form.confirmed) return 'Confirm that you understand the reload will delete existing data in scope.'
    return ''
  }, [form, isFull, isReload, hasDateRange, subjectsLoading])

  function patch(field, value) { setForm(previous => ({ ...previous, [field]: value })) }
  function chooseMode(mode) {
    setForm(previous => ({ ...previous, mode, confirmed: false, resetPackageCheck: mode !== 'NORMAL' }))
    setError(''); setResult(null)
  }
  async function submit(event) {
    event.preventDefault()
    if (validation || running) return
    setRunning(true); setError(''); setResult(null)
    try {
      const response = await fileIngestionApi.reload({
        limFormat: form.limFormat.trim().toUpperCase(), mode: form.mode,
        fromDate: usesDateRange ? form.fromDate : null, toDate: usesDateRange ? form.toDate : null,
        resetPackageCheck: form.resetPackageCheck, setReadyToLoad: form.setReadyToLoad,
        confirmation: isReload ? 'RELOAD' : ''
      })
      setResult(response.result || {})
    } catch (err) { setError(err.message) } finally { setRunning(false) }
  }

  return (
    <section className="page lim-reload-page">
      <div className="lim-heading"><div><p className="eyebrow">RAW LIM / Load & Reload</p><h1>LIM Load / Reload</h1><p className="lim-subtitle">Load staged LIM files using their delivery end date parsed from the filename.</p></div><span className="reload-context">KUMO_ADMIN · KUMO_TST</span></div>
      <form className="reload-layout" onSubmit={submit}>
        <div className="reload-card">
          <div className="reload-section"><label className="reload-label" htmlFor="lim-format">LIM subject area</label><select id="lim-format" value={form.limFormat} onChange={event => patch('limFormat', event.target.value)} disabled={subjectsLoading || !subjectAreas.length}><option value="">{subjectsLoading ? 'Loading subject areas…' : 'Select subject area'}</option>{subjectAreas.map(subject => <option key={subject} value={subject}>{subject}</option>)}</select><small>Discovered from KUMO_TST.RAW_LIM tables named RAW_LIM_&lt;SUBJECT&gt;.</small>{subjectsError && <div className="reload-validation">Could not load subject areas: {subjectsError}</div>}</div>
          <fieldset className="reload-section mode-fieldset"><legend>Operation</legend><div className="reload-mode-grid">{modes.map(mode => <label key={mode.value} className={`reload-mode ${form.mode === mode.value ? 'selected' : ''} ${mode.value === 'FULL_RELOAD' ? 'danger' : ''}`}><input type="radio" name="mode" checked={form.mode === mode.value} onChange={() => chooseMode(mode.value)} /><strong>{mode.label}</strong><span>{mode.description}</span></label>)}</div></fieldset>
          {!isFull && <div className="reload-section"><span className="reload-label">Delivery end-date range <em>optional</em></span><div className="reload-date-grid"><label>From<input type="date" value={form.fromDate} onChange={event => patch('fromDate', event.target.value)} /></label><label>To<input type="date" value={form.toDate} onChange={event => patch('toDate', event.target.value)} /></label></div><small>Leave both blank to pass NULL dates. For a reload, this performs a full reload of the selected subject area. Dates refer to DLVY_END_DATE in the filename.</small></div>}
          <div className="reload-section reload-options"><label><input type="checkbox" checked={form.resetPackageCheck} onChange={event => patch('resetPackageCheck', event.target.checked)} /><span><strong>Reset package sequence control</strong><small>Clear package year and sequence values for active rows in this subject area.</small></span></label><label><input type="checkbox" checked={form.setReadyToLoad} onChange={event => patch('setReadyToLoad', event.target.checked)} /><span><strong>Set Ready to Load</strong><small>Set DW_READY_TO_LOAD_FL to TRUE and bypass the normal KUMO Monitor readiness flow.</small></span></label></div>
          {isReload && <label className={`reload-confirm ${effectiveFullReload ? 'danger' : ''}`}><input type="checkbox" checked={form.confirmed} onChange={event => patch('confirmed', event.target.checked)} /><span><strong>{effectiveFullReload ? 'Confirm full reload' : 'Confirm reload'}</strong><small>{effectiveFullReload ? `No date range is selected. This truncates RAW_LIM_${form.limFormat || '<SUBJECT>'} and cleans related metadata before reloading all staged files.` : 'Existing RAW rows and related metadata in the selected date range will be deleted before files are reloaded.'}</small></span></label>}
          {validation && <div className="reload-validation">{validation}</div>}{error && <div className="alert error">{error}</div>}
          <button className={`button primary reload-submit ${running ? 'running' : ''} ${effectiveFullReload ? 'danger' : ''}`} type="submit" disabled={Boolean(validation) || running} aria-live="polite">{running ? <><span className="reload-spinner" aria-hidden="true" /><span>Running procedure<span className="running-dots" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span></span><small>{elapsedSeconds}s</small></> : effectiveFullReload ? 'Full reload' : selectedMode.label}</button>
          {running && <div className="reload-running-note" role="status"><span className="reload-running-pulse" /><span><strong>Snowflake is processing the request</strong><small>Keep this page open. Results will appear automatically when the procedure completes.</small></span></div>}
        </div>
        <aside className="reload-side">
          <div className="reload-card reload-summary"><span className="modal-eyebrow">Execution summary</span><h2>{effectiveFullReload ? 'Full reload' : selectedMode.label}</h2><p>{effectiveFullReload ? 'No date range: all staged files will be reloaded after the subject-area RAW table is truncated.' : selectedMode.description}</p><dl><div><dt>Format</dt><dd>{form.limFormat || '—'}</dd></div><div><dt>Date scope</dt><dd>{usesDateRange ? `${form.fromDate} → ${form.toDate}` : 'NULL → all files'}</dd></div><div><dt>Force reload</dt><dd>{isReload ? 'Yes' : 'No'}</dd></div><div><dt>Reset sequence</dt><dd>{form.resetPackageCheck ? 'Yes' : 'No'}</dd></div><div><dt>Ready to load</dt><dd>{form.setReadyToLoad ? 'Yes' : 'No'}</dd></div></dl></div>
          {result && <div className="reload-card reload-result"><span className="modal-eyebrow">Procedure result</span><h2 className={result.STATUS === 'SUCCESS' ? 'success-text' : ''}>{result.STATUS || 'Completed'}</h2><div className="reload-result-grid">{Object.entries(result).map(([key, value]) => <div key={key}><span>{resultLabels[key] || key.replaceAll('_', ' ')}</span><strong>{value === null || value === '' ? '—' : String(value)}</strong></div>)}</div></div>}
        </aside>
      </form>
    </section>
  )
}
