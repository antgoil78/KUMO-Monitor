import { Fragment, useEffect, useId, useState } from 'react'
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

function Pagination({ position, page, pageCount, from, to, total, searching, onPageChange, unit = 'groups' }) {
  return <nav className={`batch-control-pagination ${position}`} aria-label={`Batch control ${unit} pagination (${position})`}>
    <span>{from.toLocaleString('sv-SE')}–{to.toLocaleString('sv-SE')} of {total.toLocaleString('sv-SE')} {searching ? 'matching ' : ''}{unit}</span>
    <div>
      <button type="button" className="small-button" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>Previous</button>
      <span>Page {page} of {pageCount}</span>
      <button type="button" className="small-button" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>Next</button>
    </div>
  </nav>
}

function useBatchData(filters, groups, refreshVersion = 0) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    // Debounce typing and ignore responses for superseded filters or pages.
    const timer = window.setTimeout(() => {
      const load = groups ? api.batchControlGroups : api.batchControl
      load(filters)
        .then(result => { if (!cancelled) setData(result) })
        .catch(err => { if (!cancelled) setError(err.message) })
        .finally(() => { if (!cancelled) setLoading(false) })
    }, filters.search ? 300 : 0)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [filters, groups, refreshVersion])

  return { data, loading, error }
}

function toggleColumn(setFilters, column) {
  setFilters(current => ({
    ...current, page: 1, sortBy: column,
    sortDirection: current.sortBy === column && current.sortDirection === 'asc' ? 'desc' : 'asc'
  }))
}

function paginationFor(data, filters, onPageChange, unit) {
  const total = data?.total || 0
  const page = data?.page || 1
  return {
    page, total, unit, onPageChange,
    pageCount: Math.max(1, Math.ceil(total / filters.pageSize)),
    from: total ? (page - 1) * filters.pageSize + 1 : 0,
    to: Math.min(page * filters.pageSize, total),
    searching: Boolean(filters.search.trim())
  }
}

function RowsPerPage({ value, onChange }) {
  return <label className="batch-control-page-size">Rows per page
    <select value={value} onChange={event => onChange(Number(event.target.value))}>
      {[50, 100, 250, 500].map(size => <option key={size} value={size}>{size}</option>)}
    </select>
  </label>
}

function TableHeader({ columns, filters, onSort, expandable = false }) {
  return <thead><tr>
    {expandable && <th scope="col" className="batch-control-expand-heading"><span className="batch-control-sr-only">Files</span></th>}
    {columns.map(column => <th key={column.key} scope="col" aria-sort={filters.sortBy === column.key ? (filters.sortDirection === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className="batch-control-sort" title={column.key} onClick={() => onSort(column.key)}>
        {column.label}<span aria-hidden="true">{filters.sortBy === column.key ? (filters.sortDirection === 'asc' ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>)}
  </tr></thead>
}

function TableCells({ columns, row }) {
  return columns.map(column => <td key={column.key} title={row[column.key] == null ? '' : String(row[column.key])} className={column.type === 'number' ? 'batch-control-number' : column.key === 'DW_BATCH_ID' ? 'batch-control-batch-id' : ''}><CellValue column={column} value={row[column.key]} /></td>)
}

function FileDetails({ group, panelId }) {
  const [filters, setFilters] = useState({ groupKey: group.GROUP_KEY, search: '', sortBy: 'DW_FILE_NM', sortDirection: 'asc', page: 1, pageSize: 100 })
  const { data, loading, error } = useBatchData(filters, false)
  const columns = data?.columns || []
  const patch = values => setFilters(current => ({ ...current, page: 1, ...values }))
  const pagination = paginationFor(data, filters, page => patch({ page }), 'files')

  return <section id={panelId} className="batch-control-file-details" aria-label={`Files for ${group.LIM}`}>
    <div className="batch-control-detail-heading"><div><strong>Files · {group.LIM}</strong><span>Ready to load {formatDateTimeSeconds(group.READY_TO_LOAD_DTTM)}</span></div></div>
    <div className="batch-control-toolbar">
      <input type="search" className="search-input" value={filters.search} maxLength={500} onChange={event => patch({ search: event.target.value })} placeholder="Search files in this group…" aria-label="Search group files" />
      <RowsPerPage value={filters.pageSize} onChange={pageSize => patch({ pageSize })} />
    </div>
    {loading ? <LoadingState>Loading group files…</LoadingState> : error ? <div className="alert error" role="alert">Could not load group files: {error} <button type="button" className="small-button" onClick={() => patch({})}>Retry</button></div> : <>
      <Pagination position="top" {...pagination} />
      <div className="batch-control-table-scroll" tabIndex={0} role="region" aria-label="Group files table">
        <table className="batch-control-table batch-control-files-table">
          <TableHeader columns={columns} filters={filters} onSort={column => toggleColumn(setFilters, column)} />
          <tbody>{data?.rows?.length ? data.rows.map(row => <tr key={JSON.stringify([row.DW_TABLE_NM, row.DW_FILE_NM, row.DW_SRC_SYST])}><TableCells columns={columns} row={row} /></tr>) : <tr><td colSpan={Math.max(columns.length, 1)} className="batch-control-empty">No files match this group or search.</td></tr>}</tbody>
        </table>
      </div>
      <Pagination position="bottom" {...pagination} />
    </>}
  </section>
}

export default function BatchControl() {
  const [filters, setFilters] = useState({ search: '', sortBy: 'READY_TO_LOAD_DTTM', sortDirection: 'desc', page: 1, pageSize: 100 })
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [expandedKey, setExpandedKey] = useState(null)
  const panelPrefix = useId()
  const { data, loading, error } = useBatchData(filters, true, refreshVersion)

  function patchFilters(patch) {
    setExpandedKey(null)
    setFilters(current => ({ ...current, page: 1, ...patch }))
  }

  function toggleSort(column) {
    setExpandedKey(null)
    toggleColumn(setFilters, column)
  }

  const columns = data?.columns || []
  const paginationProps = paginationFor(data, filters, page => patchFilters({ page }), 'groups')

  return (
    <section className="page batch-control-page">
      <PageHeader breadcrumb="LIM / Batch Control" title="Batch Control" subtitle="Batches grouped by LIM format, ready-to-load time, version and processing status. Select a group to see its files." actions={
        <button type="button" className="button ghost-refresh" onClick={() => { setExpandedKey(null); setRefreshVersion(value => value + 1) }} disabled={loading}>↻ Refresh</button>
      } />
      <div className="batch-control-toolbar">
        <input type="search" className="search-input" value={filters.search} maxLength={500} onChange={event => patchFilters({ search: event.target.value })} placeholder="Search batch groups…" aria-label="Search batch control" />
        <RowsPerPage value={filters.pageSize} onChange={pageSize => patchFilters({ pageSize })} />
      </div>
      {error && <div className="alert error" role="alert">Could not load batch control: {error}</div>}
      <div className="table-card batch-control-card" aria-busy={loading}>
        {loading ? <LoadingState>Loading batch control…</LoadingState> : !error && <>
          <Pagination position="top" {...paginationProps} />
          <div className="batch-control-table-scroll" tabIndex={0} role="region" aria-label="Batch control table">
            <table className="batch-control-table">
              <TableHeader columns={columns} filters={filters} onSort={toggleSort} expandable />
              <tbody>{data?.rows?.length ? data.rows.map(row => {
                const expanded = expandedKey === row.GROUP_KEY
                const panelId = `${panelPrefix}-${row.GROUP_KEY}`
                return <Fragment key={row.GROUP_KEY}>
                  <tr className={`batch-control-group-row ${expanded ? 'expanded' : ''}`} onClick={() => setExpandedKey(current => current === row.GROUP_KEY ? null : row.GROUP_KEY)}>
                    <td className="batch-control-expand-cell"><button type="button" className="batch-control-expand" aria-expanded={expanded} aria-controls={expanded ? panelId : undefined} aria-label={`${expanded ? 'Hide' : 'Show'} files for ${row.LIM}, ready to load ${formatDateTimeSeconds(row.READY_TO_LOAD_DTTM)}`}><span aria-hidden="true">{expanded ? '▾' : '▸'}</span></button></td>
                    <TableCells columns={columns} row={row} />
                  </tr>
                  {expanded && <tr className="batch-control-detail-row"><td colSpan={columns.length + 1}><FileDetails group={row} panelId={panelId} /></td></tr>}
                </Fragment>
              }) : <tr><td colSpan={columns.length + 1} className="batch-control-empty">{filters.search.trim() ? 'No batch groups match your search.' : 'No batch control records found.'}</td></tr>}</tbody>
            </table>
          </div>
          <Pagination position="bottom" {...paginationProps} />
        </>}
      </div>
    </section>
  )
}
