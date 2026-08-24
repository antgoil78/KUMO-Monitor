import { useEffect, useMemo, useState } from 'react'
import { fileIngestionApi } from '../fileIngestionApi.js'
import './FileIngestion.css'

const HISTORY_DAYS = 30
const ATTENTION_STATUSES = new Set(['FAILED', 'STOPPED', 'SKIPPED', 'BLOCKED'])

function formatValue(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback
  return String(value)
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date)
}

function pct(numerator, denominator) {
  const n = Number(numerator || 0)
  const d = Number(denominator || 0)
  return d ? Math.round((n / d) * 100) : 0
}

function statusTone(kind) {
  if (['UPDATED', 'READY'].includes(kind)) return 'success'
  if (['ATTENTION', 'ROWCOUNT_ISSUE'].includes(kind)) return 'failed'
  if (['MISSING_FILES', 'WAITING', 'LATEST_NO_UPDATE'].includes(kind)) return 'queued'
  return 'muted'
}

function statusSymbol(kind) {
  if (['UPDATED', 'READY'].includes(kind)) return '✓'
  if (['ATTENTION', 'ROWCOUNT_ISSUE'].includes(kind)) return '✕'
  if (['MISSING_FILES', 'WAITING', 'LATEST_NO_UPDATE'].includes(kind)) return '●'
  return '—'
}

function logTone(status) {
  if (status === 'UPDATED') return 'success'
  if (ATTENTION_STATUSES.has(status)) return 'failed'
  if (status) return 'queued'
  return 'muted'
}

function Metric({ label, value, detail, tone = '' }) {
  return (
    <div className={`lim-metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  )
}

function SummaryStrip({ summary }) {
  const engineTone = summary.engineStatus === 'READY'
    ? 'success'
    : summary.engineStatus === 'ATTENTION'
      ? 'failed'
      : 'queued'

  return (
    <div className="lim-summary-strip">
      <span className={`lim-engine ${engineTone}`}>
        <span className="lim-engine-dot" />
        Ingestion: <strong>{summary.engineStatus}</strong>
      </span>
      <span className="lim-divider" />
      <span className="lim-summary-item success">✓ <strong>{summary.readyGroups}</strong> ready groups</span>
      <span className="lim-summary-item success">↻ <strong>{summary.updatedGroups}</strong> updated latest run</span>
      <span className="lim-summary-item queued">● <strong>{summary.missingGroups}</strong> missing-file groups</span>
      <span className="lim-summary-item failed">✕ <strong>{summary.attentionGroups}</strong> attention</span>
      <span className="lim-summary-item running">▶ <strong>{summary.waitingGroups}</strong> waiting/checked</span>
      <span className="lim-divider" />
      <span className="lim-summary-total">
        <strong>{summary.totalGroups}</strong> groups · <strong>{summary.readyFiles}/{summary.totalFiles}</strong> ready file rows · <strong>{summary.readinessPct}%</strong> · <strong>{summary.missingFiles}</strong> missing files
      </span>
    </div>
  )
}

function OverviewTable({ rows, onOpenDetail }) {
  const [expanded, setExpanded] = useState(() => new Set())
  function toggle(group) {
    setExpanded(previous => {
      const next = new Set(previous)
      if (next.has(group)) next.delete(group)
      else next.add(group)
      return next
    })
  }
  return (
    <div className="table-card lim-overview-card">
      <div className="lim-table-scroll">
        <table className="lim-table lim-overview-table">
          <thead>
            <tr>
              <th>Subject area</th>
              <th>Package group</th>
              <th>Status</th>
              <th>Latest run</th>
              <th>Files</th>
              <th>RAW status</th>
              <th>Delivery</th>
              <th>History</th>
              <th>Loading history</th>
            </tr>
          </thead>
          <tbody>
            {rows.flatMap(row => {
              const isExpanded = expanded.has(row.PKG_GROUP_NAME)
              const parent = <OverviewRow key={row.PKG_GROUP_NAME} row={row} expanded={isExpanded} onToggle={() => toggle(row.PKG_GROUP_NAME)} onOpenDetail={onOpenDetail} />
              const children = isExpanded ? (row.sources || []).map(source => <OverviewRow key={`${row.PKG_GROUP_NAME}-${source.SOURCE_ID}`} row={source} source onOpenDetail={onOpenDetail} />) : []
              return [parent, ...children]
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function OverviewRow({ row, source = false, expanded = false, onToggle, onOpenDetail }) {
  const fileRows = Number(row.FILE_ROWS || 0)
  const readyRows = Number(row.READY_ROWS || 0)
  const waitingRows = Number(row.NOT_READY_ROWS || 0)
  const readiness = pct(readyRows, fileRows)
  const tone = statusTone(row.STATUS_KIND)
  return (
    <tr className={source ? 'lim-source-row' : 'lim-subject-row'}>
      <td className="lim-muted">{source ? <span className="lim-source-branch">↳</span> : <button type="button" className="lim-expand-button" onClick={onToggle} aria-expanded={expanded}><span>{expanded ? '−' : '+'}</span>{formatValue(row.SUBJECT_AREA)}</button>}</td>
      <td>{source ? <strong className="lim-source-name">{formatValue(row.SOURCE_ID)}</strong> : <strong className="lim-package-name">{formatValue(row.PKG_GROUP_NAME)}</strong>}</td>
      <td><span className={`lim-status ${tone}`}><span>{statusSymbol(row.STATUS_KIND)}</span>{formatValue(row.STATUS_LABEL, 'No data')}</span></td>
      <td><div className="lim-primary-value">{formatValue(row.LATEST_STATUS_LIST, 'No latest run')}</div><div className="lim-sub-value">Ready run: {formatDate(row.LATEST_CONTROL_DATE)}</div></td>
      <td><div><strong>{fileRows}</strong> <span className="lim-muted">in RAW</span></div><div className="lim-sub-value">Loaded: {formatDate(row.RAW_LATEST_LOAD_DTTM)}</div></td>
      <td><div><span className="success-text">{readyRows} ready</span> <span className="lim-muted">·</span> <span className={waitingRows ? 'lim-warning-text' : 'lim-muted'}>{waitingRows} waiting</span></div><div className="lim-progress"><span style={{ width: `${readiness}%` }} /></div></td>
      <td className="lim-muted lim-nowrap">{formatDate(row.LATEST_DLVY_END_DATE)}</td>
      <td>{source ? <span className="lim-muted">—</span> : <><div>{Number(row.HISTORY_DAYS || 0)} days</div><div className="lim-sub-value">{Number(row.HISTORY_ROWS || 0)} rows</div></>}</td>
      <td><div className="lim-row-actions"><button type="button" onClick={() => onOpenDetail(row.PKG_GROUP_NAME, 'history', source ? row.SOURCE_ID : null)}>Open history</button></div></td>
    </tr>
  )
}

function DetailMetrics({ type, metrics = {} }) {
  if (type === 'raw') {
    return (
      <div className="lim-detail-metrics five">
        <Metric label="Rows" value={metrics.rows || 0} />
        <Metric label="Received" value={metrics.received || 0} tone="success" />
        <Metric label="Missing" value={metrics.missing || 0} tone={metrics.missing ? 'queued' : ''} />
        <Metric label="DW ready" value={metrics.dwReady || 0} tone="success" />
        <Metric label="Rowcount issues" value={metrics.rowcountIssues || 0} tone={metrics.rowcountIssues ? 'failed' : ''} />
      </div>
    )
  }

  if (type === 'ready') {
    return (
      <div className="lim-detail-metrics four">
        <Metric label="Rows" value={metrics.rows || 0} />
        <Metric label="Updated" value={metrics.updated || 0} tone="success" />
        <Metric label="Attention" value={metrics.attention || 0} tone={metrics.attention ? 'failed' : ''} />
        <Metric label="Rows updated" value={metrics.rowsUpdated || 0} />
      </div>
    )
  }

  return (
    <div className="lim-detail-metrics four">
      <Metric label="History rows" value={metrics.rows || 0} />
      <Metric label="Run days" value={metrics.runDays || 0} />
      <Metric label="Updated" value={metrics.updated || 0} tone="success" />
      <Metric label="Attention" value={metrics.attention || 0} tone={metrics.attention ? 'failed' : ''} />
    </div>
  )
}

function RawTable({ rows }) {
  return (
    <div className="lim-table-scroll lim-detail-scroll">
      <table className="lim-table lim-detail-table">
        <thead>
          <tr>
            <th>Group</th><th>End date</th><th>Source</th><th>Pkg ID</th><th>Year</th><th>Seq</th>
            <th>File</th><th>Type</th><th>Received</th><th>DW Ready</th><th>RC Status</th>
            <th>Expected</th><th>Actual</th><th>Loaded at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const rc = formatValue(row.ROWCOUNT_STATUS, '')
            const rcTone = rc === 'ROWCOUNT_OK' ? 'success' : rc ? 'failed' : 'muted'
            return (
              <tr key={`${row.FILE_NAME || 'row'}-${index}`}>
                <td><strong>{formatValue(row.PKG_GROUP_NAME)}</strong></td>
                <td className="lim-nowrap">{formatDate(row.DLVY_END_DATE)}</td>
                <td><strong>{formatValue(row.DLVY_SOURCE_ID)}</strong></td>
                <td>{formatValue(row.DLVY_PKG_ID)}</td>
                <td>{formatValue(row.DLVY_PKG_YEAR)}</td>
                <td>{formatValue(row.DLVY_PKG_YEAR_SEQ_NO)}</td>
                <td className="lim-reason">{formatValue(row.FILE_NAME)}</td>
                <td>{formatValue(row.FILE_TYPE)}</td>
                <td><span className={`lim-boolean ${row.RECEIVED_FL === true ? 'success' : 'failed'}`}>{row.RECEIVED_FL === true ? '✓' : '✕'}</span></td>
                <td><span className={`lim-boolean ${row.DW_READY_TO_LOAD_FL === true ? 'success' : 'failed'}`}>{row.DW_READY_TO_LOAD_FL === true ? '✓' : '✕'}</span></td>
                <td><span className={`lim-log-status ${rcTone}`}>{rc || '—'}</span></td>
                <td>{formatValue(row.EXPECTED_ROWS)}</td>
                <td>{formatValue(row.ACTUAL_ROWS)}</td>
                <td className="lim-nowrap">{formatDate(row.LOADED_AT)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function LogTable({ rows }) {
  return (
    <div className="lim-table-scroll lim-detail-scroll">
      <table className="lim-table lim-detail-table">
        <thead>
          <tr>
            <th>Control date</th><th>Group</th><th>End date</th><th>Source</th><th>Pkg ID</th>
            <th>Year</th><th>Seq</th><th>Status</th><th>Rows updated</th><th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const status = formatValue(row.STATUS, '')
            return (
              <tr key={`${row.CONTROL_DATE || 'row'}-${row.DLVY_SOURCE_ID || ''}-${index}`}>
                <td className="lim-nowrap">{formatDate(row.CONTROL_DATE)}</td>
                <td><strong>{formatValue(row.PKG_GROUP_NAME)}</strong></td>
                <td className="lim-nowrap">{formatDate(row.DLVY_END_DATE)}</td>
                <td><strong>{formatValue(row.DLVY_SOURCE_ID)}</strong></td>
                <td>{formatValue(row.DLVY_PKG_ID)}</td>
                <td>{formatValue(row.DLVY_PKG_YEAR)}</td>
                <td>{formatValue(row.DLVY_PKG_YEAR_SEQ_NO)}</td>
                <td><span className={`lim-log-status ${logTone(status)}`}>{status || '—'}</span></td>
                <td>{formatValue(row.ROWS_UPDATED)}</td>
                <td className="lim-reason">{formatValue(row.REASON)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function CompactHistoryTable({ rows, firstError }) {
  return (
    <div className="lim-history-table-wrap">
      <table className="lim-table lim-history-table">
        <thead><tr><th>Delivery</th><th>Source</th><th>Package</th><th>Status</th><th>Reason</th></tr></thead>
        <tbody>{rows.map((row, index) => {
          const status = formatValue(row.STATUS, '')
          const isFirstError = row === firstError
          return (
            <tr key={`${row.CONTROL_DATE || 'run'}-${row.DLVY_END_DATE || ''}-${row.DLVY_SOURCE_ID || ''}-${index}`} className={isFirstError ? 'lim-first-error-row' : status === 'BLOCKED' ? 'lim-blocked-row' : ''}>
              <td className="lim-nowrap">{formatDate(row.DLVY_END_DATE)}</td>
              <td><strong>{formatValue(row.DLVY_SOURCE_ID)}</strong></td>
              <td><div>{formatValue(row.DLVY_PKG_ID)}</div><div className="lim-sub-value">{formatValue(row.DLVY_PKG_YEAR)} · {formatValue(row.DLVY_PKG_YEAR_SEQ_NO)}</div></td>
              <td><span className={`lim-log-status ${logTone(status)}`}>{status || '—'}</span>{isFirstError && <span className="lim-root-cause-badge">First error</span>}</td>
              <td className="lim-history-reason">{formatValue(row.REASON)}</td>
            </tr>
          )
        })}</tbody>
      </table>
    </div>
  )
}

function HistoryGroups({ rows }) {
  const groups = useMemo(() => {
    const map = new Map()
    const sorted = [...rows].sort((a, b) => {
      const runOrder = new Date(b.CONTROL_DATE || 0) - new Date(a.CONTROL_DATE || 0)
      if (runOrder) return runOrder
      const deliveryOrder = new Date(a.DLVY_END_DATE || 0) - new Date(b.DLVY_END_DATE || 0)
      if (deliveryOrder) return deliveryOrder
      return String(a.DLVY_SOURCE_ID || '').localeCompare(String(b.DLVY_SOURCE_ID || ''))
    })
    sorted.forEach(row => {
      const key = formatValue(row.CONTROL_DATE, 'Unknown run')
      if (!map.has(key)) map.set(key, [])
      map.get(key).push(row)
    })
    return Array.from(map.entries())
  }, [rows])

  return (
    <div className="lim-history-groups">
      {groups.map(([runDate, runRows], index) => {
        const updated = runRows.filter(row => row.STATUS === 'UPDATED').length
        const attention = runRows.filter(row => ATTENTION_STATUSES.has(row.STATUS)).length
        const firstError = runRows.find(row => ATTENTION_STATUSES.has(row.STATUS) && row.STATUS !== 'BLOCKED')
          || runRows.find(row => ATTENTION_STATUSES.has(row.STATUS))
        return (
          <details key={runDate} className={`lim-history-day ${firstError ? 'has-error' : ''}`} open={index === 0}>
            <summary>
              <strong>{formatDate(runDate)}</strong>
              <span>{runRows.length} deliveries · {updated} updated · {attention} attention</span>
            </summary>
            {firstError && <div className="lim-first-error-summary"><span>First error to solve</span><strong>{formatValue(firstError.STATUS)} · {formatDate(firstError.DLVY_END_DATE)} · {formatValue(firstError.DLVY_SOURCE_ID)}</strong><p>{formatValue(firstError.REASON)}</p></div>}
            <CompactHistoryTable rows={runRows} firstError={firstError} />
          </details>
        )
      })}
    </div>
  )
}

function DetailModal({ detail, onClose }) {
  if (!detail) return null

  return (
    <div className="modal-backdrop lim-modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      <div className="vision-modal lim-detail-modal" role="dialog" aria-modal="true">
        <div className="modal-header">
          <div>
            <span className="modal-eyebrow">LIM ingestion</span>
            <h2>Loading history · {detail.groupName}{detail.sourceId ? ` · ${detail.sourceId}` : ''}</h2>
            <p>Newest runs first · deliveries ordered oldest to newest within each run · last {HISTORY_DAYS} days</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>

        {detail.loading ? (
          <div className="lim-detail-loading">Loading history...</div>
        ) : detail.error ? (
          <div className="alert error">{detail.error}</div>
        ) : (
          <>
            <DetailMetrics type={detail.type} metrics={detail.data?.metrics} />
            {detail.data?.rows?.length ? (
              <HistoryGroups rows={detail.data.rows} />
            ) : (
              <div className="lim-empty-detail">No rows to display.</div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

export default function FileIngestion() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const result = await fileIngestionApi.overview(HISTORY_DAYS)
      setData(result)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const filteredRows = useMemo(() => {
    const rows = data?.overview || []
    const term = query.trim().toLowerCase()
    if (!term) return rows
    return rows.filter(row =>
      [row.SUBJECT_AREA, row.PKG_GROUP_NAME, row.STATUS_LABEL, row.LATEST_STATUS_LIST]
        .some(value => String(value || '').toLowerCase().includes(term))
    )
  }, [data, query])

  async function openDetail(groupName, type, sourceId = null) {
    setDetail({ groupName, sourceId, type, loading: true, error: null, data: null })
    try {
      const result = type === 'raw'
        ? await fileIngestionApi.raw(groupName, sourceId)
        : type === 'ready'
          ? await fileIngestionApi.ready(groupName, sourceId)
          : await fileIngestionApi.history(groupName, HISTORY_DAYS, sourceId)
      setDetail({ groupName, sourceId, type, loading: false, error: null, data: result })
    } catch (err) {
      setDetail({ groupName, sourceId, type, loading: false, error: err.message, data: null })
    }
  }

  const emptySummary = {
    engineStatus: 'READY', totalGroups: 0, attentionGroups: 0, missingGroups: 0,
    updatedGroups: 0, readyGroups: 0, waitingGroups: 0, totalFiles: 0,
    readyFiles: 0, missingFiles: 0, readinessPct: 0
  }

  return (
    <section className="page lim-page">
      <div className="lim-heading">
        <div>
          <p className="eyebrow">RAW LIM</p>
          <h1>File Ingestion Monitor</h1>
          <p className="lim-subtitle">
            Monitor LIM package groups, file readiness, rowcount checks and SET_READY processing.
          </p>
        </div>
        <button type="button" className="ghost-refresh" onClick={load} disabled={loading}>
          {loading ? 'Refreshing...' : '↻ Refresh'}
        </button>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="lim-context-row">
        <span>
          Monitoring <strong>{data?.summary?.totalGroups || 0}</strong> active package groups across{' '}
          <strong>{data?.subjectAreas || 0}</strong> subject areas
        </span>
        <span>History window: last <strong>{data?.historyDays || HISTORY_DAYS}</strong> days</span>
      </div>

      <SummaryStrip summary={data?.summary || emptySummary} />

      <div className="lim-section-heading">
        <div>
          <h2>Ingestion overview</h2>
          <span>{filteredRows.length} package groups</span>
        </div>
        <input
          className="search-input lim-search"
          value={query}
          onChange={event => setQuery(event.target.value)}
          placeholder="Search subject area, package group or status..."
        />
      </div>

      {loading && !data ? (
        <div className="table-card lim-empty-state">Loading ingestion overview...</div>
      ) : !filteredRows.length ? (
        <div className="table-card lim-empty-state">No active package groups found.</div>
      ) : (
        <OverviewTable rows={filteredRows} onOpenDetail={openDetail} />
      )}

      <DetailModal detail={detail} onClose={() => setDetail(null)} />
    </section>
  )
}
