import { useEffect, useMemo, useState } from 'react'

import { api } from '../api.js'
import StatusBadge from '../components/StatusBadge.jsx'
import { elapsedDuration, formatDateTime } from '../utils/time.js'
import './ExecutionLog.css'

const sourceDefinitions = [
  { key: 'runLog', label: 'Run log' },
  { key: 'executionProgress', label: 'Execution progress' },
  { key: 'executionResult', label: 'Execution result' }
]

const preferredColumns = {
  runLog: ['LOG_DTTM', 'ORIGIN', 'TYPE', 'MESSAGE'],
  executionProgress: ['SRT', 'MODEL_NAME', 'MODEL_NAME_PARENT', 'STATUS', 'STATUS_DTTM', 'LOG_DTTM'],
  executionResult: ['MODEL_NAME', 'STATUS', 'START_TIME', 'LATEST_CHANGE', 'ELAPSED_S', 'ERROR_MESSAGE', 'LATEST_SQL', 'SQL']
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
  if (/\b(WARN|WARNING|SKIPPED)\b/.test(text)) return 'warning'
  if (/\b(SUCCESS|SUCCEEDED|COMPLETED|DONE|OK)\b/.test(text)) return 'success'
  if (/\b(RUNNING|EXECUTING|IN_PROGRESS|STARTED)\b/.test(text)) return 'running'
  if (/\b(INFO|DEBUG|TRACE|NOTICE)\b/.test(text)) return 'info'
  return ''
}

function columnsFor(rows, sourceKey) {
  const available = new Set(rows.flatMap(row => Object.keys(row)))
  const preferred = (preferredColumns[sourceKey] || []).filter(column => available.has(column))
  const remaining = Array.from(available).filter(column => column !== 'RUN_ID' && !preferred.includes(column))
  return [...preferred, ...remaining]
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
  const columns = columnsFor(rows, sourceKey)
  if (!rows.length) return <div className="execution-log-empty">No rows found for this run.</div>

  return (
    <div className="execution-log-table-wrap">
      <table className="workflow-table compact execution-log-table">
        <thead><tr>{columns.map(column => <th key={column}>{column.replaceAll('_', ' ')}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.LOG_ID || row.SRT || row.LOG_DTTM || index}-${index}`} className={`execution-log-row ${logTone(row.TYPE)}`}>
              {columns.map(column => {
                const value = row[column]
                const structured = structuredValue(value)
                const opensViewer = column !== 'MODEL_NAME_PARENT' && (['LATEST_SQL', 'SQL'].includes(column) || structured || String(value ?? '').length > 240)
                const tone = column === 'TYPE' ? logTone(value) : ''
                return (
                  <td key={column} className={`${['MESSAGE', 'ERROR_MESSAGE'].includes(column) ? 'execution-log-message-cell' : opensViewer ? 'execution-view-cell' : ''} ${column === 'MODEL_NAME_PARENT' ? 'execution-parent-cell' : ''} ${tone}`}>
                    {column === 'STATUS' ? <StatusBadge status={value} /> : column === 'MODEL_NAME_PARENT' ? <ParentModels value={value} /> : structured ? <FriendlyJson value={value} /> : <span>{opensViewer ? valuePreview(value) : displayValue(column, value)}</span>}
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

export default function ExecutionLog({ runId = '', workflowId = '', workflowName = '', returnPage = 'monitor', onNavigate }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeSource, setActiveSource] = useState('runLog')
  const [search, setSearch] = useState('')
  const [nowMs, setNowMs] = useState(Date.now())
  const [valueDetail, setValueDetail] = useState(null)

  async function load() {
    if (!runId) return
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

  useEffect(() => { load() }, [runId, workflowId])

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const history = data?.history || {}
  const resolvedName = history.WORKFLOW_NAME || workflowName || history.WORKFLOW_ID || 'Workflow execution'
  const historyMessage = history.MESSAGE || history.ERROR_MESSAGE || ''
  const visibleRows = useMemo(() => {
    const rows = data?.[activeSource] || []
    const query = search.trim().toLowerCase()
    if (!query) return rows
    return rows.filter(row => Object.values(row).some(value => String(value ?? '').toLowerCase().includes(query)))
  }, [data, activeSource, search])

  if (!runId) {
    return <section className="page"><div className="alert warning">No execution run was selected.</div><button className="button" onClick={() => onNavigate('monitor')}>Back to monitor</button></section>
  }

  return (
    <section className="page execution-log-page">
      <div className="page-hero execution-log-hero">
        <div>
          <p className="breadcrumb">Pages / Execution log</p>
          <h1 className="page-heading">{resolvedName}</h1>
          <p className="page-subtitle">Run <code>{runId}</code></p>
        </div>
        <div className="execution-log-header-actions">
          <button className="button" onClick={load} disabled={loading}>↻ Refresh</button>
          <button className="button" onClick={() => onNavigate(returnPage, returnPage === 'history' ? { workflowName, workflowId } : {})}>← Back</button>
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {loading && !data && <div className="empty-state">Loading execution log...</div>}

      {data && <>
        <div className="execution-log-summary vision-card-flat">
          <div><span>Status</span><StatusBadge status={history.STATUS || '—'} /></div>
          <div><span>Requested by</span><strong>{history.REQUESTED_BY || 'Unknown'}</strong></div>
          <div><span>Started</span><strong>{formatDateTime(history.START_TIME || history.REQUESTED_AT)}</strong></div>
          <div><span>Execution time</span><strong>{elapsedDuration(history.START_TIME || history.REQUESTED_AT, history.END_TIME, history.STATUS, nowMs)}</strong></div>
          <div><span>Trigger</span><strong>{history.TRIGGER_SOURCE || '—'}</strong></div>
        </div>

        <div className={`execution-history-message vision-card-flat ${historyMessage ? 'has-message' : ''}`}>
          <span>Workflow history message</span>
          <p>{historyMessage || 'No message recorded in workflow history.'}</p>
        </div>

        <div className="execution-log-source-card vision-card-flat">
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
          <LogTable rows={visibleRows} sourceKey={activeSource} onViewValue={setValueDetail} />
        </div>
      </>}
      <ValueViewer detail={valueDetail} onClose={() => setValueDetail(null)} />
    </section>
  )
}
