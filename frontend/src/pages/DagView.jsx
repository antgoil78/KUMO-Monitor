import { useEffect, useMemo, useRef, useState } from 'react'
import { Background, Controls, MarkerType, MiniMap, ReactFlow } from '@xyflow/react'
import dagre from '@dagrejs/dagre'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import LoadingState from '../components/LoadingState.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import StatusBadge, { statusKind } from '../components/StatusBadge.jsx'

function isViewModel(node) {
  return String(node?.label || node?.id || '').trim().toUpperCase().endsWith('_V')
}

function dbtSelections(command) {
  const parts = String(command || '').trim().split(/\s+/).filter(Boolean)
  const selectionIndex = parts.findIndex(part => ['-s', '--select'].includes(part))
  if (selectionIndex < 0) return []
  const selections = []
  for (const part of parts.slice(selectionIndex + 1)) {
    if (part.startsWith('-')) break
    selections.push(part.replace(/^['"]|['"]$/g, ''))
  }
  return selections
}

function collapseViewModelEdges(rawNodes, rawEdges) {
  const byId = new Map(rawNodes.map(node => [String(node.id), node]))
  const outgoing = new Map()
  for (const edge of rawEdges) {
    const source = String(edge.source)
    if (!outgoing.has(source)) outgoing.set(source, [])
    outgoing.get(source).push(String(edge.target))
  }

  const collapsed = new Map()
  for (const sourceNode of rawNodes) {
    if (isViewModel(sourceNode)) continue
    const source = String(sourceNode.id)
    const queue = [...(outgoing.get(source) || [])]
    const visitedViews = new Set()
    while (queue.length) {
      const target = queue.shift()
      const targetNode = byId.get(target)
      if (!targetNode) continue
      if (!isViewModel(targetNode)) {
        if (source !== target) collapsed.set(`${source}\u0000${target}`, { source, target, collapsedViewModels: true })
        continue
      }
      if (visitedViews.has(target)) continue
      visitedViews.add(target)
      queue.push(...(outgoing.get(target) || []))
    }
  }
  return Array.from(collapsed.values())
}

function affectedGraph(nodes, edges, nodeId, includeUpstream, includeDownstream) {
  if (!nodeId) return { nodes: [], edges: [] }
  const selectedIds = new Set([String(nodeId)])
  const upstream = new Map()
  const downstream = new Map()
  for (const edge of edges || []) {
    const source = String(edge.source)
    const target = String(edge.target)
    if (!upstream.has(target)) upstream.set(target, [])
    if (!downstream.has(source)) downstream.set(source, [])
    upstream.get(target).push(source)
    downstream.get(source).push(target)
  }
  const collect = connections => {
    const queue = [String(nodeId)]
    while (queue.length) {
      const current = queue.shift()
      for (const related of connections.get(current) || []) {
        if (selectedIds.has(related)) continue
        selectedIds.add(related)
        queue.push(related)
      }
    }
  }
  if (includeUpstream) collect(upstream)
  if (includeDownstream) collect(downstream)
  return {
    nodes: (nodes || []).filter(node => selectedIds.has(String(node.id))),
    edges: (edges || []).filter(edge => selectedIds.has(String(edge.source)) && selectedIds.has(String(edge.target)))
  }
}

function layoutGraph(rawNodes, rawEdges, direction, highlightRunSelection = false) {
  const horizontal = direction === 'LR'
  const defaultSize = { width: 190, height: 64 }
  const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
  layout.setGraph({ rankdir: direction, ranksep: 100, nodesep: 38, marginx: 40, marginy: 40 })
  const ids = new Set(rawNodes.map(node => String(node.id)))
  const edges = rawEdges.filter(edge => ids.has(String(edge.source)) && ids.has(String(edge.target)))
  rawNodes.forEach(node => layout.setNode(String(node.id), { ...defaultSize }))
  edges.forEach(edge => layout.setEdge(String(edge.source), String(edge.target)))
  dagre.layout(layout)

  return {
    nodes: rawNodes.map(node => {
      const position = layout.node(String(node.id))
      const kind = statusKind(node.status)
      const compact = isViewModel(node)
      return {
        id: String(node.id),
        position: { x: position.x - defaultSize.width / 2, y: position.y - defaultSize.height / 2 },
        data: {
          label: <><span className={`dag-graph-dot ${kind}`} /><span title={node.label || node.id}>{node.label || node.id}</span><small>{String(node.status || 'UNKNOWN')}</small>{node.testsTotal > 0 && <em className={`dag-test-count ${node.testsFailed ? 'failed' : 'passed'}`}>{node.testsFailed ? `${node.testsFailed}/${node.testsTotal} tests failed` : `${node.testsTotal} tests passed`}</em>}</>,
          refreshKey: [node.status, node.progress, node.modelStatus, node.testsTotal, node.testsFailed, node.testsWarning].join(':')
        },
        className: `dag-graph-node ${kind}${compact ? ' view-model' : ''}${highlightRunSelection ? ' run-selected' : ''}`,
        sourcePosition: horizontal ? 'right' : 'bottom',
        targetPosition: horizontal ? 'left' : 'top',
        style: { width: defaultSize.width, height: defaultSize.height }
      }
    }),
    edges: edges.map((edge, index) => {
      const target = rawNodes.find(node => String(node.id) === String(edge.target))
      const kind = statusKind(target?.status)
      const color = kind === 'failed' ? '#ff4b6e' : '#4779c9'
      return {
        id: `edge-${edge.source}-${edge.target}-${index}`,
        source: String(edge.source),
        target: String(edge.target),
        type: 'smoothstep',
        animated: kind === 'running',
        markerEnd: { type: MarkerType.ArrowClosed, color },
        style: { stroke: color, strokeWidth: edge.collapsedViewModels ? 1.9 : 1.6, strokeDasharray: edge.collapsedViewModels ? '6 4' : undefined }
      }
    })
  }
}

export default function DagView({ workflow, workflowId, workflowName, partialRun = null, historicalRunId = '', returnPage = 'monitor', onNavigate }) {
  const id = workflow?.workflowId || workflowId
  const name = workflow?.workflowName || workflowName || 'DBT workflow'
  const [dag, setDag] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const refreshingRef = useRef(false)
  const [selectedModelId, setSelectedModelId] = useState('')
  const [includeRelated, setIncludeRelated] = useState(true)
  const [showViewModels, setShowViewModels] = useState(false)
  const [statusFilter, setStatusFilter] = useState('')
  const [direction, setDirection] = useState('LR')
  const [selectedNode, setSelectedNode] = useState(null)
  const [notice, setNotice] = useState('')
  const [modelAction, setModelAction] = useState('run')
  const [includeUpstream, setIncludeUpstream] = useState(false)
  const [includeDownstream, setIncludeDownstream] = useState(false)
  const partialRunRef = useRef(partialRun)
  const isHistorical = Boolean(historicalRunId && !partialRun)
  if (partialRun && partialRunRef.current?.requestId !== partialRun.requestId) {
    partialRunRef.current = partialRun
  }

  function blankPartialDag(request, status = 'INITIATING', runId = 'pending') {
    return {
      run: { RUN_ID: runId, STATUS: status },
      nodes: (request?.nodes || []).map(node => ({ ...node, status: 'QUEUED', modelStatus: null, progress: 'QUEUED', testsTotal: 0, testsFailed: 0, testsWarning: 0 })),
      edges: request?.edges || [],
      tests: [], errors: [], modelDagComplete: false, partial: true
    }
  }

  function mergePartialDag(request, liveDag) {
    const liveById = new Map((liveDag?.nodes || []).map(node => [String(node.id), node]))
    const preview = blankPartialDag(request, liveDag?.run?.STATUS || 'QUEUED', liveDag?.run?.RUN_ID || 'pending')
    return {
      ...liveDag,
      partial: true,
      nodes: preview.nodes.map(node => ({ ...node, ...(liveById.get(String(node.id)) || {}) })),
      edges: preview.edges
    }
  }

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setError(null)
    if (partialRun) {
      setDag(blankPartialDag(partialRun))
      api.runWorkflow(id, name, true, partialRun.command).then(result => {
        if (!cancelled) {
          if (result.runId && result.runId !== 'pending') partialRunRef.current.resolvedRunId = result.runId
          setDag(current => ({ ...current, run: { RUN_ID: result.runId || 'pending', STATUS: result.status || 'INITIATING' } }))
        }
      }).catch(err => {
        if (!cancelled) setError(err.message || String(err))
      })
      return () => { cancelled = true }
    }
    setDag(null)
    api.workflowDag(id, historicalRunId).then(data => {
      if (!cancelled && !partialRunRef.current) setDag(data)
    }).catch(err => !cancelled && setError(err.message))
    return () => { cancelled = true }
  }, [id, partialRun?.requestId, historicalRunId])

  async function refreshDag() {
    if (!id || refreshingRef.current) return
    refreshingRef.current = true
    setRefreshing(true)
    setError(null)
    try {
      const currentPartialRun = partialRunRef.current
      if (currentPartialRun && !currentPartialRun.resolvedRunId) {
        // Never query the ambiguous "latest" DAG here. Resolve this request's
        // real run ID from the live lock/event stream first, then pin to it.
        const realtime = await api.realtimeState()
        const candidates = [...(realtime.locks || []), ...(realtime.events || [])]
        const match = candidates.find(item => {
          if (String(item.workflowId || '') !== String(id)) return false
          const candidateId = String(item.runId || '')
          if (!candidateId || candidateId === 'pending' || candidateId === String(currentPartialRun.previousRunId || '')) return false
          const eventTime = Date.parse(item.rememberedAt || item.requestedAt || item.lastRequestedAt || '')
          return !Number.isFinite(eventTime) || eventTime >= Number(currentPartialRun.startedAt || 0) - 2000
        })
        if (!match) return
        currentPartialRun.resolvedRunId = match.runId
      }
      const data = await api.workflowDag(id, currentPartialRun?.resolvedRunId || '')
      const nextDag = currentPartialRun ? mergePartialDag(currentPartialRun, data) : data
      setDag(nextDag)
      setSelectedNode(current => current ? (nextDag.nodes || []).find(node => String(node.id) === String(current.id)) || null : null)
    } catch (err) {
      setError(err.message)
    } finally {
      refreshingRef.current = false
      setRefreshing(false)
    }
  }

  useEffect(() => {
    if (!id || isHistorical) return undefined
    const timer = window.setInterval(refreshDag, 5000)
    return () => window.clearInterval(timer)
  }, [id, partialRun?.requestId, isHistorical])

  const allNodes = dag?.nodes || []
  const modelOptions = useMemo(() => [...allNodes]
    .filter(node => !isViewModel(node))
    .sort((left, right) => String(left.label || left.id).localeCompare(String(right.label || right.id))), [allNodes])
  const affectedPreview = useMemo(
    () => selectedNode ? affectedGraph(allNodes, dag?.edges || [], selectedNode.id, includeUpstream, includeDownstream) : null,
    [allNodes, dag?.edges, selectedNode, includeUpstream, includeDownstream]
  )
  const visibleNodes = useMemo(() => {
    if (partialRun) return allNodes.filter(node => !statusFilter || statusKind(node.status) === statusFilter)
    if (affectedPreview) return affectedPreview.nodes.filter(node => !statusFilter || statusKind(node.status) === statusFilter)
    const relatedIds = new Set()
    if (selectedModelId) {
      relatedIds.add(String(selectedModelId))
      if (includeRelated) {
        const upstreamByNode = new Map()
        const downstreamByNode = new Map()
        for (const edge of dag?.edges || []) {
          const source = String(edge.source)
          const target = String(edge.target)
          if (!upstreamByNode.has(target)) upstreamByNode.set(target, [])
          if (!downstreamByNode.has(source)) downstreamByNode.set(source, [])
          upstreamByNode.get(target).push(source)
          downstreamByNode.get(source).push(target)
        }
        const collectDirection = (connections) => {
          const queue = [String(selectedModelId)]
          const visited = new Set(queue)
          while (queue.length) {
            const nodeId = queue.shift()
            for (const relatedId of connections.get(nodeId) || []) {
              if (visited.has(relatedId)) continue
              visited.add(relatedId)
              relatedIds.add(relatedId)
              queue.push(relatedId)
            }
          }
        }
        collectDirection(upstreamByNode)
        collectDirection(downstreamByNode)
      }
    }
    return allNodes.filter(node => {
      const matchesSearch = !selectedModelId || relatedIds.has(String(node.id))
      const matchesViewSetting = showViewModels || !isViewModel(node)
      return matchesSearch && matchesViewSetting && (!statusFilter || statusKind(node.status) === statusFilter)
    })
  }, [allNodes, dag?.edges, selectedModelId, includeRelated, showViewModels, statusFilter, affectedPreview, partialRun])
  const visibleEdges = useMemo(() => {
    if (partialRun) return dag?.edges || []
    if (affectedPreview) return affectedPreview.edges
    return showViewModels ? (dag?.edges || []) : collapseViewModelEdges(allNodes, dag?.edges || [])
  }, [allNodes, dag?.edges, showViewModels, affectedPreview, partialRun])
  const graph = useMemo(() => layoutGraph(visibleNodes, visibleEdges, direction, Boolean(selectedNode && !partialRun)), [visibleNodes, visibleEdges, direction, selectedNode, partialRun])
  const graphViewKey = `${direction}|${selectedModelId}|${includeRelated}|${showViewModels}|${statusFilter}|${graph.nodes.map(node => `${node.id}:${node.data.refreshKey}`).join(',')}`
  const counts = useMemo(() => allNodes.reduce((result, node) => {
    const kind = statusKind(node.status)
    result[kind] = (result[kind] || 0) + 1
    return result
  }, {}), [allNodes])
  const complete = counts.success || 0
  const failed = counts.failed || 0
  const warning = counts.warning || 0
  const skipped = counts.skipped || 0
  const finished = complete + failed + warning + skipped
  const testsTotal = allNodes.reduce((total, node) => total + Number(node.testsTotal || 0), 0)
  const testsFailed = allNodes.reduce((total, node) => total + Number(node.testsFailed || 0), 0)
  const percent = allNodes.length ? Math.round((finished / allNodes.length) * 100) : 0

  function modelCommand(node) {
    const selector = `${includeUpstream ? '+' : ''}${node?.id || ''}${includeDownstream ? '+' : ''}`
    // The DAG response is the authoritative source for the workflow command.
    // Navigation context is retained as a fallback for older API responses.
    const originalSelections = dbtSelections(dag?.dbtCommand || workflow?.dbtCommand)
    const selections = originalSelections.length
      ? originalSelections.map(original => `${original},${selector}`)
      : [selector]
    return `dbt ${modelAction} --select ${selections.join(' ')}`
  }

  function partialPreview(node) {
    return affectedGraph(dag?.nodes || [], dag?.edges || [], node.id, includeUpstream, includeDownstream)
  }

  function startSelectedModel() {
    if (!selectedNode) return
    const command = modelCommand(selectedNode)
    const preview = partialPreview(selectedNode)
    const request = {
      requestId: `${Date.now()}-${selectedNode.id}`,
      startedAt: Date.now(),
      command,
      previousRunId: dag?.run?.RUN_ID || '',
      ...preview
    }
    // Invalidate any normal-DAG request already in flight before navigation is
    // rendered, so it cannot replace the new queued preview with the old run.
    partialRunRef.current = request
    setSelectedNode(null)
    setDag(blankPartialDag(request))
    onNavigate('dag', {
      // History opens the DAG with the scalar workflowId/workflowName props,
      // so `workflow` is not always available to carry into this DAG-to-DAG
      // navigation. Preserve both forms to keep the partial run associated
      // with its workflow regardless of where the DAG was opened.
      workflow: workflow || { workflowId: id, workflowName: name },
      workflowId: id,
      workflowName: name,
      partialRun: request
    })
  }

  if (!id) return <section className="page dag-page"><PageHeader breadcrumb="Pages / Workflow Monitor / DAG" title="Latest DAG Run" subtitle="No workflow was selected." actions={<button className="button" onClick={() => onNavigate('monitor')}>← Back to monitor</button>} /><div className="alert warning">Select a workflow from Workflow Monitor to view its DAG.</div></section>

  const pageTitle = partialRun ? 'DAG Run - Partial' : isHistorical ? 'Historical DAG Run' : 'Latest DAG Run'
  const subtitle = partialRun
    ? `${name} · DAG Run Preview`
    : isHistorical
      ? `${name} · Run ID ${historicalRunId}`
      : `${name} · interactive DBT model dependencies`
  const backPage = isHistorical ? returnPage : 'monitor'
  const backLabel = isHistorical && returnPage === 'history' ? 'history' : 'monitor'

  return (
    <section className="page dag-page">
      <PageHeader breadcrumb={isHistorical ? 'Pages / History / DAG' : 'Pages / Workflow Monitor / DAG'} title={pageTitle} subtitle={subtitle} actions={<div className="dag-header-actions"><span className={`dag-auto-refresh ${refreshing ? 'refreshing' : ''}`}><i />{isHistorical ? 'Pinned run · no refresh' : refreshing ? 'Updating…' : 'Auto refresh · 5s'}</span><button className="button" onClick={() => onNavigate(backPage, backPage === 'history' ? { workflowName: name, workflowId: id } : {})}>← Back to {backLabel}</button></div>} />
      {error && <div className="alert error">{error}</div>}
      {notice && <div className="alert info">{notice}</div>}
      {!dag && !error && <LoadingState>Loading DAG…</LoadingState>}
      {dag && <>
        <div className="dag-page-summary vision-card-flat">
          <StatusBadge status={dag.run?.STATUS || '—'} />
          <span>Run ID <code>{dag.run?.RUN_ID || '—'}</code></span>
          <strong>{allNodes.length}</strong><span>models</span>
          <strong className="success-text">{finished}</strong><span>finished</span>
          {warning > 0 && <><strong>{warning}</strong><span>warnings</span></>}
          {failed > 0 && <><strong className="failed-text">{failed}</strong><span>errors</span></>}
          {skipped > 0 && <><strong>{skipped}</strong><span>skipped</span></>}
          {testsTotal > 0 && <><strong className={testsFailed ? 'failed-text' : 'success-text'}>{testsFailed ? `${testsFailed}/${testsTotal}` : testsTotal}</strong><span>{testsFailed ? 'tests failed' : 'tests passed'}</span></>}
          <div className="dag-page-progress"><ProgressBar progress={{ percent, total: allNodes.length, done: finished, failed }} status={dag.run?.STATUS} /></div>
        </div>
        <div className="dag-page-toolbar">
          <select className="dag-model-filter" value={selectedModelId} onChange={event => setSelectedModelId(event.target.value)} aria-label="Filter by model name">
            <option value="">All model names</option>
            {modelOptions.map(node => <option key={node.id} value={node.id}>{node.label || node.id}</option>)}
          </select>
          <label className={`dag-related-filter ${includeRelated ? 'active' : ''}`}>
            <input type="checkbox" checked={includeRelated} onChange={event => setIncludeRelated(event.target.checked)} disabled={!selectedModelId} />
            <span>Show all upstream + downstream</span>
          </label>
          <label className={`dag-related-filter ${showViewModels ? 'active' : ''}`}>
            <input type="checkbox" checked={showViewModels} onChange={event => setShowViewModels(event.target.checked)} />
            <span>Show _V models</span>
          </label>
          <select className="status-select" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
            <option value="">All statuses</option><option value="success">Success</option><option value="warning">Warning</option><option value="running">Started</option><option value="failed">Error</option><option value="queued">Queued</option><option value="skipped">Skipped</option>
          </select>
          <div className="view-switch"><button className={direction === 'LR' ? 'active' : ''} onClick={() => setDirection('LR')}>Left → right</button><button className={direction === 'TB' ? 'active' : ''} onClick={() => setDirection('TB')}>Top → bottom</button></div>
          <span className="dag-visible-count">Showing {visibleNodes.length} of {allNodes.length}</span>
        </div>
        <div className="dag-page-canvas">
          {graph.nodes.length ? <ReactFlow key={graphViewKey} nodes={graph.nodes} edges={graph.edges} fitView fitViewOptions={{ padding: 0.2 }} minZoom={0.08} maxZoom={2} nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, node) => setSelectedNode(allNodes.find(item => String(item.id) === node.id))}>
            <Background color="rgba(105, 139, 255, 0.18)" gap={22} size={1} />
            <MiniMap pannable zoomable nodeColor={node => node.className?.includes('failed') ? '#ff4b6e' : node.className?.includes('success') ? '#01b574' : node.className?.includes('running') ? '#0075ff' : '#647695'} maskColor="rgba(3, 9, 31, 0.72)" />
            <Controls showInteractive={false} />
          </ReactFlow> : <div className="soft-empty">No models match the current filters.</div>}
          {selectedNode && <aside className="dag-model-popover">
            <button className="dag-model-close" onClick={() => setSelectedNode(null)} aria-label="Close model details">×</button>
            <span className="modal-eyebrow">Selected model</span>
            <h3>{selectedNode.label}</h3>
            <code>{selectedNode.id}</code>
            <StatusBadge status={selectedNode.status} />
            <div className="dag-model-metrics">
              <span>Type <strong>{selectedNode.type || '—'}</strong></span>
              <span>Progress <strong>{selectedNode.progress || 'QUEUED'}</strong></span>
              <span>Model status <strong>{selectedNode.modelStatus || '—'}</strong></span>
              <span>Tests <strong className={selectedNode.testsFailed ? 'failed-text' : ''}>{selectedNode.testsTotal ? `${selectedNode.testsTotal - selectedNode.testsFailed - (selectedNode.testsWarning || 0)} success · ${selectedNode.testsWarning || 0} warning · ${selectedNode.testsFailed} error` : 'None'}</strong></span>
            </div>
            <div className="dag-model-run-controls">
              <label><span>Action</span><select value={modelAction} onChange={event => setModelAction(event.target.value)}><option value="run">Run</option><option value="build">Build</option><option value="test">Test</option></select></label>
              <div className="dag-model-direction-options">
                <label className={includeUpstream ? 'active' : ''}><input type="checkbox" checked={includeUpstream} onChange={event => setIncludeUpstream(event.target.checked)} /><span>UPSTREAM</span></label>
                <label className={includeDownstream ? 'active' : ''}><input type="checkbox" checked={includeDownstream} onChange={event => setIncludeDownstream(event.target.checked)} /><span>DOWNSTREAM</span></label>
              </div>
              <div className="dag-model-affected-count">{affectedPreview?.nodes.length || 1} affected model{affectedPreview?.nodes.length === 1 ? '' : 's'}</div>
              <div className="dag-model-command"><span>Command</span><code>{modelCommand(selectedNode)}</code></div>
            </div>
            <div className="dag-model-actions">
              <button className="button primary" onClick={startSelectedModel}>{`▶ ${modelAction[0].toUpperCase()}${modelAction.slice(1)} model`}</button>
            </div>
            <small>This starts a new workflow run with child workflows disabled.</small>
          </aside>}
        </div>
        {dag.errors?.length > 0 && <div className="modal-table-wrap dag-error-table"><table className="workflow-table compact"><thead><tr><th>Time</th><th>Model</th><th>Error</th></tr></thead><tbody>{dag.errors.map((item, index) => <tr key={`${item.ORIGIN}-${index}`}><td>{item.LOG_DTTM || '—'}</td><td>{item.ORIGIN}</td><td>{String(item.MESSAGE || '').slice(0, 300)}</td></tr>)}</tbody></table></div>}
      </>}
    </section>
  )
}
