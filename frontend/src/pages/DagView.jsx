import { useEffect, useMemo, useState } from 'react'
import { Background, Controls, MarkerType, MiniMap, ReactFlow } from '@xyflow/react'
import dagre from '@dagrejs/dagre'

import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import ProgressBar from '../components/ProgressBar.jsx'
import StatusBadge, { statusKind } from '../components/StatusBadge.jsx'

function layoutGraph(rawNodes, rawEdges, direction) {
  const horizontal = direction === 'LR'
  const nodeWidth = 190
  const nodeHeight = 64
  const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
  layout.setGraph({ rankdir: direction, ranksep: 100, nodesep: 38, marginx: 40, marginy: 40 })
  const ids = new Set(rawNodes.map(node => String(node.id)))
  const edges = rawEdges.filter(edge => ids.has(String(edge.source)) && ids.has(String(edge.target)))
  rawNodes.forEach(node => layout.setNode(String(node.id), { width: nodeWidth, height: nodeHeight }))
  edges.forEach(edge => layout.setEdge(String(edge.source), String(edge.target)))
  dagre.layout(layout)

  return {
    nodes: rawNodes.map(node => {
      const position = layout.node(String(node.id))
      const kind = statusKind(node.status)
      return {
        id: String(node.id),
        position: { x: position.x - nodeWidth / 2, y: position.y - nodeHeight / 2 },
        data: { label: <><span className={`dag-graph-dot ${kind}`} /><span>{node.label}</span><small>{String(node.status || 'UNKNOWN')}</small></> },
        className: `dag-graph-node ${kind}`,
        sourcePosition: horizontal ? 'right' : 'bottom',
        targetPosition: horizontal ? 'left' : 'top',
        style: { width: nodeWidth, height: nodeHeight }
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
        style: { stroke: color, strokeWidth: 1.6 }
      }
    })
  }
}

export default function DagView({ workflow, workflowId, workflowName, onNavigate }) {
  const id = workflow?.workflowId || workflowId
  const name = workflow?.workflowName || workflowName || 'DBT workflow'
  const [dag, setDag] = useState(null)
  const [error, setError] = useState(null)
  const [selectedModelId, setSelectedModelId] = useState('')
  const [includeRelated, setIncludeRelated] = useState(true)
  const [statusFilter, setStatusFilter] = useState('')
  const [direction, setDirection] = useState('LR')
  const [selectedNode, setSelectedNode] = useState(null)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setDag(null)
    setError(null)
    api.workflowDag(id, workflow?.lastRunId).then(data => !cancelled && setDag(data)).catch(err => !cancelled && setError(err.message))
    return () => { cancelled = true }
  }, [id, workflow?.lastRunId])

  const allNodes = dag?.nodes || []
  const modelOptions = useMemo(() => [...allNodes]
    .sort((left, right) => String(left.label || left.id).localeCompare(String(right.label || right.id))), [allNodes])
  const visibleNodes = useMemo(() => {
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
      return matchesSearch && (!statusFilter || statusKind(node.status) === statusFilter)
    })
  }, [allNodes, dag?.edges, selectedModelId, includeRelated, statusFilter])
  const graph = useMemo(() => layoutGraph(visibleNodes, dag?.edges || [], direction), [visibleNodes, dag?.edges, direction])
  const graphViewKey = `${direction}|${selectedModelId}|${includeRelated}|${statusFilter}|${graph.nodes.map(node => node.id).join(',')}`
  const counts = useMemo(() => allNodes.reduce((result, node) => {
    const kind = statusKind(node.status)
    result[kind] = (result[kind] || 0) + 1
    return result
  }, {}), [allNodes])
  const complete = counts.success || 0
  const failed = counts.failed || 0
  const percent = allNodes.length ? Math.round((complete / allNodes.length) * 100) : 0

  function placeholder(action) {
    setNotice(`${action} is a placeholder and is not connected yet.`)
    window.setTimeout(() => setNotice(''), 3500)
  }

  if (!id) return <section className="page dag-page"><PageHeader breadcrumb="Pages / Workflow Monitor / DAG" title="DAG Run" subtitle="No workflow was selected." actions={<button className="button" onClick={() => onNavigate('monitor')}>← Back to monitor</button>} /><div className="alert warning">Select a workflow from Workflow Monitor to view its DAG.</div></section>

  return (
    <section className="page dag-page">
      <PageHeader breadcrumb="Pages / Workflow Monitor / DAG" title="DAG Run" subtitle={`${name} · interactive DBT model dependencies`} actions={<button className="button" onClick={() => onNavigate('monitor')}>← Back to monitor</button>} />
      {error && <div className="alert error">{error}</div>}
      {notice && <div className="alert info">{notice}</div>}
      {!dag && !error && <div className="empty-state">Loading DAG...</div>}
      {dag && <>
        <div className="dag-page-summary vision-card-flat">
          <StatusBadge status={dag.run?.STATUS || '—'} />
          <span>Run <code>{dag.run?.RUN_ID || '—'}</code></span>
          <strong>{allNodes.length}</strong><span>models</span>
          <strong className="success-text">{complete}</strong><span>completed</span>
          {failed > 0 && <><strong className="failed-text">{failed}</strong><span>failed</span></>}
          <div className="dag-page-progress"><ProgressBar progress={{ percent, total: allNodes.length, done: complete, failed }} status={dag.run?.STATUS} /></div>
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
          <select className="status-select" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
            <option value="">All statuses</option><option value="success">Success</option><option value="running">Running</option><option value="failed">Failed</option><option value="queued">Queued</option>
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
            <div className="dag-model-actions">
              <button className="button primary" onClick={() => placeholder('Restart model')}>↻ Restart</button>
              <button className="button" onClick={() => placeholder('View log')}>▤ View log</button>
            </div>
            <small>Actions are preview placeholders.</small>
          </aside>}
        </div>
        {dag.errors?.length > 0 && <div className="modal-table-wrap dag-error-table"><table className="workflow-table compact"><thead><tr><th>Time</th><th>Model</th><th>Error</th></tr></thead><tbody>{dag.errors.map((item, index) => <tr key={`${item.ORIGIN}-${index}`}><td>{item.LOG_DTTM || '—'}</td><td>{item.ORIGIN}</td><td>{String(item.MESSAGE || '').slice(0, 300)}</td></tr>)}</tbody></table></div>}
      </>}
    </section>
  )
}
