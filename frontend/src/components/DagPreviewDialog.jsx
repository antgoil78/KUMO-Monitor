import { useEffect, useMemo, useState } from 'react'
import { Background, Controls, MarkerType, MiniMap, ReactFlow } from '@xyflow/react'
import dagre from '@dagrejs/dagre'
import { api } from '../api.js'
import EditorDialog from './EditorDialog.jsx'

export default function DagPreviewDialog({ definitionId, name, orchestration = false, onClose }) {
  const [preview, setPreview] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let cancelled = false
    setPreview(null)
    setError(null)
    const request = orchestration ? api.previewOrchestrationJobDag(definitionId) : api.previewWorkflowDag(definitionId)
    request
      .then(data => !cancelled && setPreview(data))
      .catch(err => !cancelled && setError(err.message))
    return () => { cancelled = true }
  }, [definitionId, orchestration])
  const graph = useMemo(() => {
    const nodes = preview?.nodes || []
    const nodeWidth = 205
    const nodeHeight = 68
    const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
    layout.setGraph({ rankdir: 'LR', ranksep: 95, nodesep: 38, marginx: 30, marginy: 30 })
    const nodeIds = new Set(nodes.map(node => String(node.id)))
    const edges = (preview?.edges || []).filter(edge => nodeIds.has(String(edge.source)) && nodeIds.has(String(edge.target)))
    nodes.forEach(node => layout.setNode(String(node.id), { width: nodeWidth, height: nodeHeight }))
    edges.forEach(edge => layout.setEdge(String(edge.source), String(edge.target)))
    dagre.layout(layout)
    return {
      nodes: nodes.map(node => {
        const position = layout.node(String(node.id))
        return {
          id: String(node.id),
          position: { x: position.x - nodeWidth / 2, y: position.y - nodeHeight / 2 },
          data: { label: <><span className="dag-graph-dot running" /><span title={node.path}>{node.label}</span><small>{node.materialization}{node.tests ? ` · ${node.tests} test${node.tests === 1 ? '' : 's'}` : ''}</small></> },
          className: 'dag-graph-node preview',
          sourcePosition: 'right',
          targetPosition: 'left',
          style: { width: nodeWidth, height: nodeHeight }
        }
      }),
      edges: edges.map((edge, index) => ({
        id: `preview-edge-${edge.source}-${edge.target}-${index}`,
        source: String(edge.source),
        target: String(edge.target),
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed, color: '#4779c9' },
        style: { stroke: '#4779c9', strokeWidth: 1.6 }
      }))
    }
  }, [preview])
  return (
    <EditorDialog title={`DAG Preview for ${name}`} onClose={onClose}>
      <div className="vision-modal wide">
        <div className="modal-header"><div><h2>DAG Preview</h2><p>{name}</p></div><button className="modal-close" type="button" aria-label="Close dialog" onClick={onClose}>×</button></div>
      {error && <div className="alert error">{error}</div>}
      {!preview && !error && <div className="empty-state">Resolving the DBT selection from the project. This can take about a minute...</div>}
      {preview && <>
        <div className="dag-summary">
          <span><strong>{preview.modelCount}</strong> models</span>
          <span><strong>{preview.testCount}</strong> tests</span>
          {preview.target && <span>Target <code>{preview.target}</code></span>}
        </div>
        <div className="preview-selection"><span>Selection</span>{(preview.selection || []).map(item => <code key={item}>{item}</code>)}</div>
        {(preview.nodes || []).length ? (
          <div className="dag-graph" aria-label="DBT selection dependency preview">
            <ReactFlow nodes={graph.nodes} edges={graph.edges} fitView fitViewOptions={{ padding: 0.18 }} minZoom={0.12} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false}>
              <Background color="rgba(105, 139, 255, 0.18)" gap={22} size={1} />
              <MiniMap pannable zoomable nodeColor="#0075ff" maskColor="rgba(3, 9, 31, 0.72)" />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        ) : <div className="soft-empty">The saved selection did not resolve any models.</div>}
      </>}
    </div>
    </EditorDialog>
  )
}
