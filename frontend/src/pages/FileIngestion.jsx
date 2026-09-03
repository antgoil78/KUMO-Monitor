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

function displayStatus(value) {
  return String(value || '').replaceAll('UPDATED', 'READY').replaceAll('Updated', 'Ready')
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
      <span className="lim-summary-item success">✓ <strong>{summary.updatedGroups}</strong> ready latest run</span>
      <span className="lim-summary-item failed">✕ <strong>{summary.attentionGroups}</strong> attention</span>
      <span className="lim-summary-item running">▶ <strong>{summary.waitingGroups}</strong> waiting/checked</span>
      <span className="lim-summary-total"><strong>{summary.totalGroups}</strong> groups in latest run</span>
    </div>
  )
}

function OverviewTable({ rows, onOpenDetail, showRaw, rawRows }) {
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
              {showRaw && <><th>RAW files</th><th>RAW status</th></>}
              <th>View details</th>
            </tr>
          </thead>
          <tbody>
            {rows.flatMap(row => {
              const isExpanded = expanded.has(row.PKG_GROUP_NAME)
              const parentRaw = rawRows.get(`${row.PKG_GROUP_NAME}|`) || {}
              const parent = <OverviewRow key={row.PKG_GROUP_NAME} row={row} rawRow={parentRaw} showRaw={showRaw} expanded={isExpanded} onToggle={() => toggle(row.PKG_GROUP_NAME)} onOpenDetail={onOpenDetail} />
              const children = isExpanded ? (row.sources || []).map(source => <OverviewRow key={`${row.PKG_GROUP_NAME}-${source.SOURCE_ID}`} row={source} rawRow={rawRows.get(`${row.PKG_GROUP_NAME}|${source.SOURCE_ID}`) || {}} showRaw={showRaw} source onOpenDetail={onOpenDetail} />) : []
              return [parent, ...children]
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function OverviewRow({ row, rawRow = {}, showRaw = false, source = false, expanded = false, onToggle, onOpenDetail }) {
  const tone = statusTone(row.STATUS_KIND)
  const rawFiles = Number(rawRow.RAW_FILE_COUNT || 0)
  const rawReady = Number(rawRow.RAW_READY_FILES || 0)
  const rawWaiting = Number(rawRow.RAW_NOT_READY_FILES || 0)
  return (
    <tr className={source ? 'lim-source-row' : 'lim-subject-row'}>
      <td className="lim-muted">{source ? <span className="lim-source-branch">↳</span> : <button type="button" className="lim-expand-button" onClick={onToggle} aria-expanded={expanded}><span>{expanded ? '−' : '+'}</span>{formatValue(row.SUBJECT_AREA)}</button>}</td>
      <td>{source ? <strong className="lim-source-name">{formatValue(row.SOURCE_ID)}</strong> : <strong className="lim-package-name">{formatValue(row.PKG_GROUP_NAME)}</strong>}</td>
      <td><span className={`lim-status ${tone}`}><span>{statusSymbol(row.STATUS_KIND)}</span>{formatValue(row.STATUS_LABEL, 'No data')}</span></td>
      <td><div className="lim-primary-value">{displayStatus(row.LATEST_STATUS_LIST) || 'No latest run'}</div><div className="lim-sub-value">{Number(row.LATEST_LOG_ROWS || 0)} results · {formatDate(row.LATEST_CONTROL_DATE)}</div></td>
      {showRaw && <><td><strong>{rawFiles}</strong><div className="lim-sub-value">Loaded: {formatDate(rawRow.RAW_LATEST_LOAD_DTTM)}</div></td><td><span className="success-text">{rawReady} ready</span> <span className="lim-muted">·</span> <span className={rawWaiting ? 'lim-warning-text' : 'lim-muted'}>{rawWaiting} waiting</span></td></>}
      <td><div className="lim-row-actions"><button type="button" onClick={() => onOpenDetail(row.PKG_GROUP_NAME, source ? row.SOURCE_ID : null)}>View details</button></div></td>
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
        <Metric label="Ready" value={metrics.updated || 0} tone="success" />
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

function CompactHistoryTable({ rows, firstError, onResolve }) {
  return (
    <div className="lim-history-table-wrap">
      <table className="lim-table lim-history-table">
        <thead><tr><th>Delivery</th><th>Source</th><th>Package</th><th>Status</th><th>Reason</th><th>Action</th></tr></thead>
        <tbody>{rows.map((row, index) => {
          const status = formatValue(row.STATUS, '')
          const isFirstError = row === firstError
          return (
            <tr key={`${row.CONTROL_DATE || 'run'}-${row.DLVY_END_DATE || ''}-${row.DLVY_SOURCE_ID || ''}-${index}`} className={isFirstError ? 'lim-first-error-row' : status === 'BLOCKED' ? 'lim-blocked-row' : ''}>
              <td className="lim-nowrap">{formatDate(row.DLVY_END_DATE)}</td>
              <td><strong>{formatValue(row.DLVY_SOURCE_ID)}</strong></td>
              <td><div>{formatValue(row.DLVY_PKG_ID)}</div><div className="lim-sub-value">{formatValue(row.DLVY_PKG_YEAR)} · {formatValue(row.DLVY_PKG_YEAR_SEQ_NO)}</div></td>
              <td><span className={`lim-log-status ${logTone(status)}`}>{displayStatus(status) || '—'}</span>{isFirstError && <span className="lim-root-cause-badge">First error</span>}</td>
              <td className="lim-history-reason">{formatValue(row.REASON)}</td>
              <td>{isFirstError ? <button type="button" className="lim-resolve-button" onClick={() => onResolve(row)}>Resolution</button> : <span className="lim-muted">—</span>}</td>
            </tr>
          )
        })}</tbody>
      </table>
    </div>
  )
}

function sameValue(left, right) {
  if (left === null || left === undefined || right === null || right === undefined) return true
  return String(left).toUpperCase() === String(right).toUpperCase()
}

function sameDate(left, right) {
  if (!left || !right) return true
  const leftDate = new Date(left)
  const rightDate = new Date(right)
  if (Number.isNaN(leftDate.getTime()) || Number.isNaN(rightDate.getTime())) return sameValue(left, right)
  return leftDate.toISOString().slice(0, 10) === rightDate.toISOString().slice(0, 10)
}

function fileNeedsAttention(row) {
  return row.RECEIVED_FL === false
    || row.DW_READY_TO_LOAD_FL === false
    || row.IS_VALID_SEQUENCE === false
    || Boolean(row.ROWCOUNT_STATUS && row.ROWCOUNT_STATUS !== 'ROWCOUNT_OK')
}

function firstAttentionFile(logRows, rawRows) {
  const firstAttention = logRows.find(row => ATTENTION_STATUSES.has(row.STATUS) && row.STATUS !== 'BLOCKED')
    || logRows.find(row => ATTENTION_STATUSES.has(row.STATUS))
  const candidates = rawRows.filter(fileNeedsAttention)
  if (!firstAttention) return candidates[0] || null
  return candidates.find(row =>
    sameValue(row.DLVY_SOURCE_ID, firstAttention.DLVY_SOURCE_ID)
    && sameValue(row.DLVY_PKG_ID, firstAttention.DLVY_PKG_ID)
    && sameValue(row.DLVY_PKG_YEAR, firstAttention.DLVY_PKG_YEAR)
    && sameValue(row.DLVY_PKG_YEAR_SEQ_NO, firstAttention.DLVY_PKG_YEAR_SEQ_NO)
    && sameDate(row.DLVY_END_DATE, firstAttention.DLVY_END_DATE)
  ) || candidates[0] || null
}

function AttentionFile({ file }) {
  if (!file) return null
  const issues = []
  if (file.RECEIVED_FL === false) issues.push('File not received')
  if (file.DW_READY_TO_LOAD_FL === false) issues.push('Not ready for DW load')
  if (file.IS_VALID_SEQUENCE === false) issues.push('Invalid sequence')
  if (file.ROWCOUNT_STATUS && file.ROWCOUNT_STATUS !== 'ROWCOUNT_OK') issues.push(formatValue(file.ROWCOUNT_STATUS))
  return (
    <div className="lim-attention-file">
      <span>First file needing attention</span>
      <strong>{formatValue(file.FILE_NAME)}</strong>
      <p>{issues.join(' · ') || 'Needs attention'} · {formatValue(file.DLVY_SOURCE_ID)} · delivery {formatDate(file.DLVY_END_DATE)}</p>
    </div>
  )
}

function FirstErrorSummary({ error, file, onResolve }) {
  if (!error) return null
  return (
    <div className="lim-first-error-card">
      <div className="lim-first-error-content">
        <span>First error to resolve</span>
        <strong>{formatValue(error.STATUS)} · {formatValue(error.DLVY_SOURCE_ID)} · delivery {formatDate(error.DLVY_END_DATE)}</strong>
        <p>{formatValue(error.REASON)}</p>
        {file && <small>File: {formatValue(file.FILE_NAME)}</small>}
      </div>
      <button type="button" className="lim-resolve-button prominent" onClick={() => onResolve(error)}>Resolution</button>
    </div>
  )
}

function RemainingResults({ rows }) {
  const [expanded, setExpanded] = useState(false)
  if (!rows.length) return null
  const blocked = rows.filter(row => row.STATUS === 'BLOCKED').length
  return (
    <details className="lim-remaining-results" onToggle={event => setExpanded(event.currentTarget.open)}>
      <summary>
        <div><strong>Remaining results</strong><span>Expand to load the rest of this run</span></div>
        <span>{rows.length} rows{blocked ? ` · ${blocked} blocked` : ''}</span>
      </summary>
      {expanded && <CompactHistoryTable rows={rows} firstError={null} onResolve={() => {}} />}
    </details>
  )
}

function sqlString(value) {
  return String(value ?? '').replaceAll("'", "''")
}

function previousSequence(value) {
  const text = String(value ?? '')
  const number = Number.parseInt(text, 10)
  return Number.isFinite(number) && number > 0 ? String(number - 1).padStart(text.length, '0') : text
}

function resolutionFor(error, file) {
  const reason = String(error?.REASON || '').toUpperCase()
  const isSequence = reason.includes('INVALID_SEQUENCE') || file?.IS_VALID_SEQUENCE === false
  const isRowcount = reason.includes('ROWCOUNT') || Boolean(file?.ROWCOUNT_STATUS && file.ROWCOUNT_STATUS !== 'ROWCOUNT_OK')

  if (isSequence) {
    const source = sqlString(error.DLVY_SOURCE_ID)
    const group = sqlString(error.PKG_GROUP_NAME)
    const receivedYear = sqlString(error.DLVY_PKG_YEAR)
    const receivedSequence = sqlString(error.DLVY_PKG_YEAR_SEQ_NO)
    const configuredYear = sqlString(error.CONFIGURED_PKG_YEAR)
    const configuredSequence = sqlString(error.CONFIGURED_SEQUENCE)
    const nextExpectedBase = previousSequence(error.DLVY_PKG_YEAR_SEQ_NO)
    return {
      title: 'Correct the expected package sequence',
      description: `The received sequence ${receivedSequence} cannot follow the configured sequence ${configuredSequence || '—'}. Review the gap, then update the stored sequence to ${nextExpectedBase} so ${receivedSequence} becomes the next expected package.`,
      warning: 'Review whether the missing sequence is intentionally skipped before running this update.',
      sql: `-- Review the current configured sequence\nSELECT PKG_GROUP_NAME, SOURCE_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO, UPDATED_AT\nFROM KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE\nWHERE PKG_GROUP_NAME = '${group}'\n  AND UPPER(SOURCE_ID) = '${source}';\n\n-- Advance the stored sequence immediately before the received package\nUPDATE KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE\nSET DLVY_PKG_YEAR = '${receivedYear}',\n    DLVY_PKG_YEAR_SEQ_NO = '${nextExpectedBase}',\n    UPDATED_AT = CURRENT_TIMESTAMP()\nWHERE PKG_GROUP_NAME = '${group}'\n  AND UPPER(SOURCE_ID) = '${source}'\n  AND DLVY_PKG_YEAR = '${configuredYear}'\n  AND DLVY_PKG_YEAR_SEQ_NO = '${configuredSequence}';`
    }
  }

  if (isRowcount && file) {
    const fileName = sqlString(file.FILE_NAME)
    const rawTable = /^[A-Z0-9_$.]+$/i.test(file.RAW_TABLE || '') ? file.RAW_TABLE : 'KUMO_TST.RAW_LIM.RAW_LIM_CACT'
    return {
      title: 'Investigate the row-count mismatch',
      description: `The control metadata expects ${formatValue(file.EXPECTED_ROWS)} rows, while RAW contains ${formatValue(file.ACTUAL_ROWS)}. Run these read-only statements in a Snowflake worksheet to inspect the discrepancy.`,
      sql: `SET file_name = '${fileName}';\n\n-- Expected and detected row counts from LIM metadata\nSELECT FILE_NAME, EXPECTED_ROWS, DATA_ROWS, ACTUAL_ROWS, ROWCOUNT_STATUS\nFROM KUMO_TST.RAW_LIM.RAW_LIM_META\nWHERE FILE_NAME = $file_name\nORDER BY LOADED_AT DESC;\n\n-- Inspect the rows loaded for this file\nSELECT *\nFROM ${rawTable}\nWHERE REGEXP_REPLACE(DW_FILE_NM, '\\\\.gz$', '', 1, 0, 'i') = $file_name\nLIMIT 200;\n\n-- Aggregate the total loaded row count\nSELECT REGEXP_REPLACE(DW_FILE_NM, '\\\\.gz$', '', 1, 0, 'i') AS FILE_NAME, COUNT(*) AS ACTUAL_ROW_COUNT\nFROM ${rawTable}\nWHERE REGEXP_REPLACE(DW_FILE_NM, '\\\\.gz$', '', 1, 0, 'i') = $file_name\nGROUP BY 1;`
    }
  }

  return { title: 'Resolution not available yet', description: 'This error type does not yet have a generated resolution.', sql: '' }
}

function SuggestedResolution({ error, file, onClose }) {
  const [copied, setCopied] = useState(false)
  const resolution = resolutionFor(error, file)
  async function copySql() {
    await navigator.clipboard.writeText(resolution.sql)
    setCopied(true)
  }
  return (
    <div className="lim-resolution-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      <div className="lim-resolution-panel" role="dialog" aria-modal="true" aria-label="Suggested resolution">
        <div className="lim-resolution-header">
          <div><span>Suggested resolution</span><h3>{resolution.title}</h3></div>
          <button type="button" onClick={onClose} aria-label="Close resolution">×</button>
        </div>
        <p>{resolution.description}</p>
        {resolution.warning && <div className="alert warning">{resolution.warning}</div>}
        {resolution.sql && <><div className="lim-sql-heading"><strong>Snowflake SQL</strong><button type="button" onClick={copySql}>{copied ? 'Copied' : 'Copy SQL'}</button></div><pre className="lim-resolution-sql"><code>{resolution.sql}</code></pre></>}
      </div>
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

function LatestRunDetails({ detail, onClose, standalone = false }) {
  const [resolutionError, setResolutionError] = useState(null)
  if (!detail) return null

  const rows = detail.data?.rows || []
  const firstError = rows.find(row => ATTENTION_STATUSES.has(row.STATUS) && row.STATUS !== 'BLOCKED')
    || rows.find(row => ATTENTION_STATUSES.has(row.STATUS))
  const remainingRows = firstError ? rows.filter(row => row !== firstError) : rows
  const runDate = rows.reduce((latest, row) => {
    if (!row.CONTROL_DATE) return latest
    return !latest || new Date(row.CONTROL_DATE) > new Date(latest) ? row.CONTROL_DATE : latest
  }, null)

  const panel = (
      <div className={standalone ? 'vision-card-flat lim-detail-page-card' : 'vision-modal lim-detail-modal'}>
        <div className="modal-header">
          <div>
            <span className="modal-eyebrow">LIM ingestion</span>
            <h2>Latest run details · {detail.groupName}{detail.sourceId ? ` · ${detail.sourceId}` : ''}</h2>
            <p>{runDate ? `Run ${formatDate(runDate)}` : 'Latest run'} · showing the latest run only</p>
          </div>
          <button type="button" className={standalone ? 'button' : 'modal-close'} onClick={onClose} aria-label={standalone ? 'Back to File Ingestion Monitor' : 'Close'}>{standalone ? '← Back' : '×'}</button>
        </div>

        {detail.loading ? (
          <div className="lim-detail-loading">Loading latest run details...</div>
        ) : detail.error ? (
          <div className="alert error">{detail.error}</div>
        ) : (
          <>
            <DetailMetrics type="ready" metrics={detail.data?.metrics} />
            {firstError ? (
              <>
                <FirstErrorSummary error={firstError} file={detail.data?.attentionFile} onResolve={setResolutionError} />
                <RemainingResults rows={remainingRows} />
              </>
            ) : rows.length ? (
              <RemainingResults rows={rows} />
            ) : (
              <div className="lim-empty-detail">No rows to display.</div>
            )}
          </>
        )}
        {resolutionError && <SuggestedResolution error={resolutionError} file={detail.data?.attentionFile} onClose={() => setResolutionError(null)} />}
      </div>
  )

  if (standalone) {
    return <section className="page lim-page lim-detail-page">{panel}</section>
  }
  return (
    <div className="modal-backdrop lim-modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      {panel}
    </div>
  )
}

export function FileIngestionDetail({ groupName, sourceId = null, onNavigate }) {
  const [detail, setDetail] = useState({ groupName, sourceId, loading: true, error: null, data: null })

  useEffect(() => {
    let cancelled = false
    async function loadDetail() {
      try {
        const [latest, raw] = await Promise.all([
          fileIngestionApi.ready(groupName, sourceId),
          fileIngestionApi.raw(groupName, sourceId)
        ])
        if (!cancelled) setDetail({ groupName, sourceId, loading: false, error: null, data: { ...latest, attentionFile: firstAttentionFile(latest.rows || [], raw.rows || []) } })
      } catch (err) {
        if (!cancelled) setDetail({ groupName, sourceId, loading: false, error: err.message, data: null })
      }
    }
    loadDetail()
    return () => { cancelled = true }
  }, [groupName, sourceId])

  return <LatestRunDetails detail={detail} standalone onClose={() => onNavigate('fileIngestion')} />
}

export default function FileIngestion({ onNavigate }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [rawLoading, setRawLoading] = useState(false)
  const [rawRows, setRawRows] = useState(() => new Map())

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const result = await fileIngestionApi.overview()
      setData(result)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  function refreshAll() {
    load()
    loadRawStatus()
  }

  useEffect(() => {
    load()
    loadRawStatus()
  }, [])

  async function loadRawStatus() {
    setRawLoading(true)
    try {
      const result = await fileIngestionApi.rawStatus()
      setRawRows(new Map((result.rows || []).map(row => [
        `${row.PKG_GROUP_NAME}|${row.SOURCE_ID || ''}`,
        row
      ])))
    } catch (err) {
      setError(err.message)
    } finally {
      setRawLoading(false)
    }
  }

  const filteredRows = useMemo(() => {
    const rows = data?.overview || []
    const term = query.trim().toLowerCase()
    if (!term) return rows
    return rows.filter(row =>
      [row.SUBJECT_AREA, row.PKG_GROUP_NAME, row.STATUS_LABEL, row.LATEST_STATUS_LIST]
        .some(value => String(value || '').toLowerCase().includes(term))
    )
  }, [data, query])

  function openDetail(groupName, sourceId = null) {
    onNavigate('fileIngestionDetail', { groupName, sourceId })
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
        <button type="button" className="ghost-refresh" onClick={refreshAll} disabled={loading || rawLoading}>
          {loading || rawLoading ? 'Refreshing...' : '↻ Refresh'}
        </button>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="lim-context-row">
        <span>
          Monitoring <strong>{data?.summary?.totalGroups || 0}</strong> active package groups across{' '}
          <strong>{data?.subjectAreas || 0}</strong> subject areas
        </span>
        <span>Scope: <strong>latest SET_READY run only</strong></span>
      </div>

      <SummaryStrip summary={data?.summary || emptySummary} />

      <div className="lim-section-heading">
        <div>
          <h2>Ingestion overview</h2>
          <span>{filteredRows.length} package groups</span>
        </div>
        <div className="lim-overview-controls">
          {rawLoading && <span className="lim-raw-loading">Loading RAW status…</span>}
          <input className="search-input lim-search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search subject area, package group or status..." />
        </div>
      </div>

      {loading && !data ? (
        <div className="table-card lim-empty-state">Loading ingestion overview...</div>
      ) : !filteredRows.length ? (
        <div className="table-card lim-empty-state">No active package groups found.</div>
      ) : (
        <OverviewTable rows={filteredRows} onOpenDetail={openDetail} showRaw rawRows={rawRows} />
      )}

    </section>
  )
}
