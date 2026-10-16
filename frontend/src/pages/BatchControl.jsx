import { useEffect, useState } from 'react'
import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'
import { formatDateTimeSeconds } from '../utils/time.js'
import './BatchControl.css'

function BatchIdCopy({ value }) {
  const [status, setStatus] = useState('')

  useEffect(() => {
    if (!status) return undefined
    const timer = window.setTimeout(() => setStatus(''), 2000)
    return () => window.clearTimeout(timer)
  }, [status])

  async function copy() {
    try {
      await navigator.clipboard.writeText(String(value))
    } catch {
      const input = document.createElement('textarea')
      input.value = String(value)
      input.setAttribute('readonly', '')
      input.style.position = 'fixed'
      input.style.opacity = '0'
      document.body.appendChild(input)
      try {
        input.select()
        if (!document.execCommand('copy')) throw new Error('Copy failed')
      } catch {
        setStatus('error')
        return
      } finally {
        input.remove()
      }
    }
    setStatus('copied')
  }

  const label = status === 'copied' ? 'Copied batch ID' : status === 'error' ? 'Copy failed. Try again' : 'Copy batch ID'
  return <button type="button" className={`batch-control-copy ${status}`} title={`${label}: ${value}`} aria-label={label} onClick={copy}>
    <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.7">
      {status === 'copied' ? <path d="m5 12 4 4L19 6" /> : <><rect x="8" y="8" width="12" height="13" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" /></>}
    </svg>
  </button>
}

function CellValue({ column, value }) {
  if (value === null || value === undefined || value === '') return <span className="batch-control-muted">—</span>
  if (column.key === 'DW_BATCH_ID') return <BatchIdCopy value={value} />
  if (column.type === 'boolean') return <span className={`batch-control-flag ${value ? 'is-true' : ''}`}>{value ? 'Yes' : 'No'}</span>
  if (column.type === 'datetime') return formatDateTimeSeconds(value)
  if (column.type === 'number') return Number(value).toLocaleString('sv-SE')
  return String(value)
}

function Pagination({ position, page, pageCount, from, to, total, searching, onPageChange }) {
  return <nav className={`batch-control-pagination ${position}`} aria-label={`Batch control pagination (${position})`}>
    <span>{from.toLocaleString('sv-SE')}–{to.toLocaleString('sv-SE')} of {total.toLocaleString('sv-SE')} {searching ? 'matching ' : ''}records</span>
    <div>
      <button type="button" className="small-button" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>Previous</button>
      <span>Page {page} of {pageCount}</span>
      <button type="button" className="small-button" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>Next</button>
    </div>
  </nav>
}

export default function BatchControl() {
  const [filters, setFilters] = useState({ search: '', sortBy: 'READY_TO_LOAD_DTTM', sortDirection: 'desc', page: 1, pageSize: 100 })
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshVersion, setRefreshVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    // Debounce typing and ignore responses for superseded filters or pages.
    const timer = window.setTimeout(() => {
      api.batchControl(filters)
        .then(result => { if (!cancelled) setData(result) })
        .catch(err => { if (!cancelled) setError(err.message) })
        .finally(() => { if (!cancelled) setLoading(false) })
    }, filters.search ? 300 : 0)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [filters, refreshVersion])

  function patchFilters(patch) {
    setFilters(current => ({ ...current, page: 1, ...patch }))
  }

  function toggleSort(column) {
    setFilters(current => ({
      ...current, page: 1, sortBy: column,
      sortDirection: current.sortBy === column && current.sortDirection === 'asc' ? 'desc' : 'asc'
    }))
  }

  const columns = data?.columns || []
  const total = data?.total || 0
  const page = data?.page || 1
  const pageCount = Math.max(1, Math.ceil(total / filters.pageSize))
  const from = total ? (page - 1) * filters.pageSize + 1 : 0
  const to = Math.min(page * filters.pageSize, total)
  const paginationProps = { page, pageCount, from, to, total, searching: Boolean(filters.search.trim()), onPageChange: nextPage => patchFilters({ page: nextPage }) }

  return (
    <section className="page batch-control-page">
      <PageHeader breadcrumb="LIM / Batch Control" title="Batch Control" subtitle="Browse batch readiness, processing status and file records." actions={
        <button type="button" className="button ghost-refresh" onClick={() => setRefreshVersion(value => value + 1)} disabled={loading}>↻ Refresh</button>
      } />
      <div className="batch-control-toolbar">
        <input type="search" className="search-input" value={filters.search} maxLength={500} onChange={event => patchFilters({ search: event.target.value })} placeholder="Search across all columns…" aria-label="Search batch control" />
        <label className="batch-control-page-size">Rows per page
          <select value={filters.pageSize} onChange={event => patchFilters({ pageSize: Number(event.target.value) })}>
            {[50, 100, 250, 500].map(size => <option key={size} value={size}>{size}</option>)}
          </select>
        </label>
      </div>
      {error && <div className="alert error" role="alert">Could not load batch control: {error}</div>}
      <div className="table-card batch-control-card" aria-busy={loading}>
        {loading ? <LoadingState>Loading batch control…</LoadingState> : !error && <>
          <Pagination position="top" {...paginationProps} />
          <div className="batch-control-table-scroll" tabIndex={0} role="region" aria-label="Batch control table">
            <table className="batch-control-table">
              <thead><tr>{columns.map(column => (
                <th key={column.key} scope="col" aria-sort={filters.sortBy === column.key ? (filters.sortDirection === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <button type="button" className="batch-control-sort" title={column.key} onClick={() => toggleSort(column.key)}>
                    {column.label}<span aria-hidden="true">{filters.sortBy === column.key ? (filters.sortDirection === 'asc' ? '▲' : '▼') : '↕'}</span>
                  </button>
                </th>
              ))}</tr></thead>
              <tbody>{data?.rows?.length ? data.rows.map(row => (
                <tr key={JSON.stringify([row.DW_TABLE_NM, row.DW_FILE_NM, row.DW_SRC_SYST])}>
                  {columns.map(column => <td key={column.key} title={row[column.key] == null ? '' : String(row[column.key])} className={column.type === 'number' ? 'batch-control-number' : column.key === 'DW_BATCH_ID' ? 'batch-control-batch-id' : ''}><CellValue column={column} value={row[column.key]} /></td>)}
                </tr>
              )) : <tr><td colSpan={Math.max(columns.length, 1)} className="batch-control-empty">{filters.search.trim() ? 'No batches match your search.' : 'No batch control records found.'}</td></tr>}</tbody>
            </table>
          </div>
          <Pagination position="bottom" {...paginationProps} />
        </>}
      </div>
    </section>
  )
}
