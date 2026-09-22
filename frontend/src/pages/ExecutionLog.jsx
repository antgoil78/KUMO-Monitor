import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'
import './ExecutionLog.css'

const sourceDefinitions = [
  { key: 'modelProgress', label: 'Models' },
  { key: 'testProgress', label: 'Tests' },
  { key: 'runLog', label: 'Event log' }
]

const preferredColumns = {
  runLog: ['LOG_DTTM', 'ORIGIN', 'TYPE', 'MESSAGE'],
  modelProgress: ['MODEL_NAME', 'TYPE', 'STATUS', 'PROGRESS', 'STARTED_DTTM', 'START_DTTM', 'FINISHED_DTTM', 'FINISH_DTTM', 'MODEL_NAME_PARENT'],
  testProgress: ['MODEL_NAME', 'TYPE', 'STATUS', 'PROGRESS', 'STARTED_DTTM', 'START_DTTM', 'FINISHED_DTTM', 'FINISH_DTTM', 'MODEL_NAME_PARENT']
}

function displayValue(column, value) {
  if (value === null || value === undefined || value === '') return '—'
  if (column.includes('DTTM') || column.endsWith('_AT') || column.endsWith('_TIME')) return formatDateTime(value)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function structuredValue(value) {
  if (value && typeof value === 'object') return value
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  if (!trimmed || !['{', '['].includes(trimmed[0])) return null
  try {
    return JSON.parse(trimmed)
  } catch (_) {
    return null
  }
}

function valuePreview(value, limit = 110) {
  const structured = structuredValue(value)
  const text = structured
    ? (Array.isArray(structured) ? `${structured.length} items` : `${Object.keys(structured).length} properties`)
    : String(value ?? '')
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

function logTone(value) {
  const text = String(value ?? '').trim().toUpperCase()
  if (!text) return ''
  if (/\b(ERROR|FAILED|FAILURE|FATAL|ABORTED)\b/.test(text)) return 'failed'
  if (/\b(WARN|WARNING|VARNING|SKIPPED)\b/.test(text)) return 'warning'
  if (/\b(SUCCESS|SUCCEEDED|COMPLETED|DONE|OK)\b/.test(text)) return 'success'
  if (/\b(RUNNING|EXECUTING|IN_PROGRESS|STARTED)\b/.test(text)) return 'running'
  if (/\b(INFO|DEBUG|TRACE|NOTICE)\b/.test(text)) return 'info'
  return ''
}

function timestamp(value) {
  const parsed = Date.parse(value || '')
  return Number.isFinite(parsed) ? parsed : null
}

function executionDuration(row) {
  const started = timestamp(row.STARTED_DTTM || row.START_DTTM)
  const finished = timestamp(row.FINISHED_DTTM || row.FINISH_DTTM)
  return started !== null && finished !== null ? Math.max(0, finished - started) : null
}

function formatDuration(milliseconds) {
  if (milliseconds === null) return '—'
  if (milliseconds < 1000) return `${milliseconds} ms`
  const totalSeconds = Math.floor(milliseconds / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours) return `${hours}h ${minutes}m ${seconds}s`
  if (minutes) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

function compareValues(left, right, column) {
  if (column === 'ELAPSED_TIME') return (executionDuration(left) ?? -1) - (executionDuration(right) ?? -1)
  const a = left[column]
  const b = right[column]
  if (a === b) return 0
  if (a === null || a === undefined || a === '') return -1
  if (b === null || b === undefined || b === '') return 1
  if (column.includes('DTTM') || column.endsWith('_AT') || column.endsWith('_TIME')) {
    return (timestamp(a) ?? 0) - (timestamp(b) ?? 0)
  }
  const aNumber = typeof a === 'number' ? a : Number.NaN
  const bNumber = typeof b === 'number' ? b : Number.NaN
  if (Number.isFinite(aNumber) && Number.isFinite(bNumber)) return aNumber - bNumber
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

function columnsFor(rows, sourceKey) {
  const available = new Set(rows.flatMap(row => Object.keys(row)))
  const preferred = (preferredColumns[sourceKey] || []).filter(column => available.has(column))
  const hidden = new Set(['RUN_ID', ...(['modelProgress', 'testProgress'].includes(sourceKey) ? ['TYPE', 'FINISHED_DTTM', 'FINISH_DTTM'] : [])])
  const visiblePreferred = preferred.filter(column => !hidden.has(column))
  const remaining = Array.from(available).filter(column => !hidden.has(column) && !visiblePreferred.includes(column))
  return [...visiblePreferred, ...remaining]
}

function ValueViewer({ detail, onClose }) {
  if (!detail) return null
  const structured = structuredValue(detail.value)
  const content = structured ? JSON.stringify(structured, null, 2) : String(detail.value ?? '')

  return (
    <div className="modal-backdrop execution-value-backdrop" role="dialog" aria-modal="true" aria-labelledby="execution-value-title" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      <div className="vision-modal execution-value-modal">
        <div className="modal-header">
          <div>
            <span className="modal-eyebrow">Execution result detail</span>
            <h2 id="execution-value-title">{detail.column.replaceAll('_', ' ')}</h2>
            {detail.modelName && <p>{detail.modelName}</p>}
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <pre className={`execution-value-content ${structured ? 'json' : 'sql'}`}>{content}</pre>
      </div>
    </div>
  )
}

function FriendlyJson({ value }) {
  const structured = structuredValue(value)
  if (!structured) return <span>{displayValue('', value)}</span>
  if (Array.isArray(structured)) return <span className="execution-json-summary">Array · {structured.length} items</span>
  const entries = Object.entries(structured).slice(0, 4)
  return (
    <dl className="execution-json-preview">
      {entries.map(([key, item]) => {
        const text = typeof item === 'object' ? JSON.stringify(item) : String(item ?? '—')
        return <div key={key}><dt>{key}</dt><dd className={logTone(text)}>{text}</dd></div>
      })}
      {Object.keys(structured).length > entries.length && <div><dt>More</dt><dd>+{Object.keys(structured).length - entries.length} properties</dd></div>}
    </dl>
  )
}

function ParentModels({ value }) {
  const structured = structuredValue(value)
  const source = Array.isArray(structured) ? structured : String(value || '').split(';')
  const parents = source.map(parent => String(parent || '').trim()).filter(Boolean)
  if (!parents.length) return <span>—</span>
  return (
    <div className="execution-parent-list">
      {parents.map((parent, index) => <code key={`${parent}-${index}`}>{parent}</code>)}
    </div>
  )
}

function LogTable({ rows, sourceKey, onViewValue }) {
  const [sort, setSort] = useState({ column: '', direction: 'asc' })
  const dataColumns = columnsFor(rows, sourceKey)
  const hasTiming = ['modelProgress', 'testProgress'].includes(sourceKey)
  const startedIndex = Math.max(dataColumns.indexOf('STARTED_DTTM'), dataColumns.indexOf('START_DTTM'))
  const columns = hasTiming && startedIndex >= 0
    ? [...dataColumns.slice(0, startedIndex + 1), 'ELAPSED_TIME', ...dataColumns.slice(startedIndex + 1)]
    : dataColumns
  const sortedRows = useMemo(() => {
    if (!sort.column) return rows
    return rows.map((row, index) => ({ row, index })).sort((left, right) => {
      const result = compareValues(left.row, right.row, sort.column)
      return (result || left.index - right.index) * (sort.direction === 'asc' ? 1 : -1)
    }).map(item => item.row)
  }, [rows, sort])
  const maxDuration = Math.max(1, ...rows.map(executionDuration).filter(value => value !== null))
  function toggleSort(column) {
    setSort(current => current.column === column
      ? { column, direction: current.direction === 'asc' ? 'desc' : 'asc' }
      : { column, direction: 'asc' })
  }
  if (!rows.length) return <div className="execution-log-empty">No rows found for this run.</div>

  return (
    <div className="execution-log-table-wrap">
      <table className="workflow-table compact execution-log-table">
        <thead><tr>{columns.map(column => <th key={column} className={column === 'MODEL_NAME' ? 'execution-model-heading' : ''} aria-sort={sort.column === column ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" className="execution-sort-button" onClick={() => toggleSort(column)}>{column.replaceAll('_', ' ')}<span aria-hidden="true">{sort.column === column ? (sort.direction === 'asc' ? '▲' : '▼') : '↕'}</span></button></th>)}</tr></thead>
        <tbody>
          {sortedRows.map((row, index) => (
            <tr key={`${row.LOG_ID || row.SRT || row.LOG_DTTM || index}-${index}`} className={`execution-log-row ${logTone(row.TYPE)}`}>
              {columns.map(column => {
                if (column === 'ELAPSED_TIME') {
                  const duration = executionDuration(row)
                  const width = duration === null ? 0 : Math.max(3, (duration / maxDuration) * 100)
                  return <td key={column} className="execution-time-cell"><span>{formatDuration(duration)}</span>{duration !== null && <div className="execution-time-track" aria-hidden="true"><i style={{ width: `${width}%` }} /></div>}</td>
                }
                const value = row[column]
                const structured = structuredValue(value)
                const opensViewer = column !== 'MODEL_NAME_PARENT' && (['LATEST_SQL', 'SQL'].includes(column) || structured || String(value ?? '').length > 240)
                const tone = column === 'TYPE' ? logTone(value) : ''
                return (
                  <td key={column} className={`${['MESSAGE', 'ERROR_MESSAGE'].includes(column) ? 'execution-log-message-cell' : opensViewer ? 'execution-view-cell' : ''} ${column === 'MODEL_NAME' ? 'execution-model-cell' : ''} ${column === 'MODEL_NAME_PARENT' ? 'execution-parent-cell' : ''} ${tone}`}>
                    {column === 'STATUS' || (sourceKey === 'runLog' && column === 'TYPE') ? <StatusBadge status={value} showIcon={column === 'STATUS'} /> : column === 'MODEL_NAME_PARENT' ? <ParentModels value={value} /> : structured ? <FriendlyJson value={value} /> : <span>{opensViewer ? valuePreview(value) : displayValue(column, value)}</span>}
                    {opensViewer && <button type="button" className="execution-view-value" onClick={() => onViewValue({ column, value, modelName: row.MODEL_NAME })}>View</button>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ExecutionLog({ runId = '', workflowId = '', workflowName = '', workflow = null, returnPage = 'monitor', onNavigate }) {
  const workflowType = String(workflow?.workflowType || '').toUpperCase()
  const localData = workflow && workflowType && workflowType !== 'DBT' ? {
    history: {
      RUN_ID: runId,
      WORKFLOW_ID: workflowId,
      WORKFLOW_NAME: workflowName,
      WORKFLOW_TYPE: workflowType,
      STATUS: workflow.lastStatus,
      REQUESTED_BY: workflow.lastRequestedBy,
      REQUESTED_AT: workflow.lastRequestedAt,
      START_TIME: workflow.lastStartTime,
      END_TIME: workflow.lastEndTime,
      TRIGGER_SOURCE: workflow.lastTriggerSource,
      ERROR_MESSAGE: workflow.lastErrorMessage,
      PAYLOAD_MESSAGE: workflow.lastPayloadMessage
    },
    modelProgress: [], testProgress: [], runLog: [], warnings: {}
  } : null
  const [data, setData] = useState(localData)
  const [loading, setLoading] = useState(!localData)
  const [error, setError] = useState(null)
  const [activeSource, setActiveSource] = useState('modelProgress')
  const [search, setSearch] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [valueDetail, setValueDetail] = useState(null)
  const [runIdCopied, setRunIdCopied] = useState(false)

  async function copyRunId() {
    const value = String(runId || '')
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
    } catch (_) {
      const textArea = document.createElement('textarea')
      textArea.value = value
      textArea.setAttribute('readonly', '')
      textArea.style.position = 'fixed'
      textArea.style.opacity = '0'
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
    }
    setRunIdCopied(true)
    window.setTimeout(() => setRunIdCopied(false), 1400)
  }

  async function load() {
    if (!runId) return
    if (localData) {
      setData(localData)
      setLoading(false)
      return
    }
    try {
      setLoading(true)
      setError(null)
      setData(await api.executionLog(runId, workflowId))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [runId, workflowId, workflowType])

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const history = data?.history || {}
  const resolvedName = history.WORKFLOW_NAME || workflowName || history.WORKFLOW_ID || 'Workflow execution'
  const historyMessage = history.ERROR_MESSAGE || history.PAYLOAD_MESSAGE || ''
  const isDbtWorkflow = String(history.WORKFLOW_TYPE || 'DBT').toUpperCase() === 'DBT'
  const modelProgress = data?.modelProgress || []
  const testProgress = data?.testProgress || []
  const progressCounts = rows => rows.reduce((counts, row) => {
    const progress = String(row.PROGRESS || 'QUEUED').toUpperCase()
    const status = String(row.STATUS || '').toUpperCase()
    counts[progress.toLowerCase()] = (counts[progress.toLowerCase()] || 0) + 1
    if (status === 'ERROR') counts.errors += 1
    if (status === 'WARNING') counts.warnings += 1
    if (status === 'SUCCESS') counts.success += 1
    return counts
  }, { queued: 0, started: 0, finished: 0, skipped: 0, success: 0, warnings: 0, errors: 0 })
  const modelCounts = progressCounts(modelProgress)
  const testCounts = progressCounts(testProgress)
  const visibleRows = useMemo(() => {
    const rows = data?.[activeSource] || []
    const query = search.trim().toLowerCase()
    if (!query) return rows
    return rows.filter(row => Object.values(row).some(value => String(value ?? '').toLowerCase().includes(query)))
  }, [data, activeSource, search])

  if (!runId) {
    return <section className="page execution-log-page"><PageHeader breadcrumb="Pages / Execution Log" title="Execution Log" subtitle="No execution run was selected." actions={<button className="button" onClick={() => onNavigate('monitor')}>← Back to monitor</button>} /><div className="alert warning">Select a run from Workflow Monitor or History to view its execution log.</div></section>
  }

  return (
    <section className="page execution-log-page">
      <PageHeader breadcrumb="Pages / Execution Log" title={resolvedName} subtitle={<span className="execution-run-id">Run ID <code>{runId}</code><button type="button" className={runIdCopied ? 'copied' : ''} onClick={copyRunId} aria-label="Copy run ID" title={runIdCopied ? 'Copied' : 'Copy run ID'}><span className="execution-copy-icon" aria-hidden="true" />{runIdCopied ? 'Copied' : 'Copy'}</button></span>} actions={<div className="execution-log-header-actions">
          <button className="button" onClick={load} disabled={loading}>↻ Refresh</button>
          <button className="button" onClick={() => onNavigate(returnPage, returnPage === 'history' ? { workflowName, workflowId } : {})}>← Back</button>
        </div>} />

      {error && <div className="alert error">{error}</div>}
      {loading && !data && <LoadingState>Loading execution log…</LoadingState>}

      {data && <>
        <div className="execution-log-summary vision-card-flat">
          <div><span>Status</span><StatusBadge status={history.STATUS || '—'} /></div>
          <div><span>Requested by</span><strong>{history.REQUESTED_BY || 'Unknown'}</strong></div>
          <div><span>Started</span><strong>{formatDateTime(history.START_TIME || history.REQUESTED_AT)}</strong></div>
          <div><span>Execution time</span><strong>{elapsedDuration(history.START_TIME || history.REQUESTED_AT, history.END_TIME, history.STATUS, nowMs)}</strong></div>
          <div><span>Trigger</span><strong>{history.TRIGGER_SOURCE || '—'}</strong></div>
          {isDbtWorkflow && <div><span>Models</span><strong>{modelProgress.length ? `${modelCounts.finished} finished · ${modelCounts.started} started · ${modelCounts.queued} queued · ${modelCounts.skipped} skipped` : 'No model progress yet'}</strong></div>}
          {isDbtWorkflow && <div><span>Tests</span><strong>{testProgress.length ? `${testCounts.success} success · ${testCounts.warnings} warning · ${testCounts.errors} error · ${testCounts.queued} queued` : 'No tests recorded'}</strong></div>}
        </div>

        <div className={`execution-history-message vision-card-flat ${historyMessage ? 'has-message' : ''}`}>
          <span>Workflow history message</span>
          <p>{historyMessage || 'No message recorded in workflow history.'}</p>
        </div>

        {isDbtWorkflow && <div className="execution-log-source-card vision-card-flat">
          <div className="execution-log-toolbar">
            <div className="view-switch execution-log-tabs">
              {sourceDefinitions.map(source => (
                <button key={source.key} className={activeSource === source.key ? 'active' : ''} onClick={() => setActiveSource(source.key)}>
                  {source.label} <span>{data[source.key]?.length || 0}</span>
                </button>
              ))}
            </div>
            <input className="search-input" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search this log source..." aria-label="Search log rows" />
          </div>

          {data.warnings?.[activeSource] && <div className="alert warning compact">This source is unavailable: {data.warnings[activeSource]}</div>}
          <LogTable key={activeSource} rows={visibleRows} sourceKey={activeSource} onViewValue={setValueDetail} />
        </div>}
      </>}
      <ValueViewer detail={valueDetail} onClose={() => setValueDetail(null)} />
    </section>
  )
}
