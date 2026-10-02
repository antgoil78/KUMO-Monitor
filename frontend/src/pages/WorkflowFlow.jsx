import { useEffect, useMemo, useState } from 'react'
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, useNodesState } from '@xyflow/react'

import { api } from '../api.js'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import './WorkflowFlow.css'

const WORKFLOW_WIDTH = 300
const JOB_HEIGHT = 54
const JOB_GAP = 12

function triggerList(value) {
  if (Array.isArray(value)) return value
  return String(value || '').match(/[A-Z][A-Z0-9_]*/gi) || []
}

function relationVisual(dependency) {
  const type = String(dependency.DEPENDENCY_TYPE || 'TRIGGER').toUpperCase()
  const status = String(dependency.MIN_STATUS_LEVEL || 'SUCCESS').toUpperCase()
  if (type === 'BLOCKER') return { color: '#ef5350', port: 'red', label: `Blocks on ${status.toLowerCase()}` }
  if (status === 'WARNING') return { color: '#f2c94c', port: 'yellow', label: 'On warning' }
  if (['ERROR', 'FAILED', 'FAILURE'].includes(status)) return { color: '#ef5350', port: 'red', label: `On ${status.toLowerCase()}` }
  return { color: '#4299ff', port: 'blue', label: `On ${status.toLowerCase()}` }
}

function orderedJobs(rows) {
  const byId = new Map(rows.map(row => [String(row.JOB_ID), row]))
  const compareJobs = (a, b) => {
    const sequenceDifference = Number(byId.get(a)?.JOB_SEQUENCE || 0) - Number(byId.get(b)?.JOB_SEQUENCE || 0)
    return sequenceDifference || String(byId.get(a)?.JOB_NAME).localeCompare(String(byId.get(b)?.JOB_NAME))
  }
  const children = new Map()
  const roots = []
  rows.forEach(row => {
    const parent = String(row.PARENT_JOB_ID || '')
    if (!parent) roots.push(String(row.JOB_ID))
    else children.set(parent, [...(children.get(parent) || []), String(row.JOB_ID)])
  })
  const result = []
  const seen = new Set()
  function visit(id) {
    if (!id || seen.has(id) || !byId.has(id)) return
    seen.add(id)
    result.push(byId.get(id))
    ;(children.get(id) || []).sort(compareJobs).forEach(visit)
  }
  roots.sort(compareJobs).forEach(visit)
  rows.forEach(row => visit(String(row.JOB_ID)))
  return result
}

function buildGraph(payload) {
  const workflows = payload.workflows || []
  const definitions = payload.jobs || []
  const dependencies = payload.dependencies || []
  const workflowIds = new Set(workflows.map(row => String(row.WORKFLOW_ID)))
  const rank = new Map([...workflowIds].map(id => [id, 0]))
  for (let pass = 0; pass < workflows.length; pass += 1) {
    dependencies.forEach(dependency => {
      const parent = String(dependency.PARENT_WORKFLOW_ID)
      const child = String(dependency.CHILD_WORKFLOW_ID)
      if (workflowIds.has(parent) && workflowIds.has(child)) rank.set(child, Math.max(rank.get(child) || 0, (rank.get(parent) || 0) + 1))
    })
  }

  const inputs = new Map()
  const outputs = new Map()
  dependencies.forEach(dependency => {
    const visual = relationVisual(dependency)
    const source = String(dependency.PARENT_WORKFLOW_ID)
    const target = String(dependency.CHILD_WORKFLOW_ID)
    if (!outputs.has(source)) outputs.set(source, new Map())
    if (!inputs.has(target)) inputs.set(target, new Map())
    outputs.get(source).set(visual.port, visual)
    inputs.get(target).set(visual.port, visual)
  })

  const grouped = new Map()
  workflows.forEach(workflow => {
    const level = rank.get(String(workflow.WORKFLOW_ID)) || 0
    if (!grouped.has(level)) grouped.set(level, [])
    grouped.get(level).push(workflow)
  })

  const nodes = []
  ;[...grouped.entries()].sort(([a], [b]) => a - b).forEach(([level, group]) => {
    let y = 30
    group.sort((a, b) => String(a.WORKFLOW_NAME).localeCompare(String(b.WORKFLOW_NAME))).forEach(workflow => {
      const workflowId = String(workflow.WORKFLOW_ID)
      const jobs = orderedJobs(definitions.filter(row => String(row.WORKFLOW_ID) === workflowId))
      const height = Math.max(230, 108 + jobs.length * (JOB_HEIGHT + JOB_GAP))
      const externalTriggers = triggerList(workflow.EXTERNAL_TRIGGERS)
      nodes.push({
        id: workflowId,
        type: 'workflow',
        position: { x: 70 + level * 500, y },
        data: {
          label: workflow.WORKFLOW_NAME,
          description: externalTriggers.length ? externalTriggers.join(' · ') : 'Dependency only',
          jobCount: jobs.length,
          expandedHeight: height,
          inputs: [...(inputs.get(workflowId)?.values() || [])],
          outputs: [...(outputs.get(workflowId)?.values() || [])]
        },
        style: { width: WORKFLOW_WIDTH, height }
      })
      const jobById = new Map(jobs.map(job => [String(job.JOB_ID), job]))
      jobs.forEach((job, index) => {
        const parent = jobById.get(String(job.PARENT_JOB_ID || ''))
        nodes.push({
          id: `${workflowId}::${job.JOB_ID}`,
          type: 'job',
          parentId: workflowId,
          extent: 'parent',
          position: { x: 24, y: 88 + index * (JOB_HEIGHT + JOB_GAP) },
          data: { label: job.JOB_NAME, command: job.COMMAND_LINE, detail: parent ? `After ${parent.JOB_NAME}` : 'Root job' },
          draggable: false
        })
      })
      y += height + 70
    })
  })

  const jobEdges = definitions.filter(row => row.PARENT_JOB_ID).map(row => ({
    id: `job-${row.WORKFLOW_ID}-${row.PARENT_JOB_ID}-${row.JOB_ID}`,
    source: `${row.WORKFLOW_ID}::${row.PARENT_JOB_ID}`,
    target: `${row.WORKFLOW_ID}::${row.JOB_ID}`,
    data: { workflowId: String(row.WORKFLOW_ID) },
    type: 'smoothstep',
    className: 'workflow-flow-job-edge',
    style: { stroke: '#6b6e78', strokeWidth: 1.4 },
    markerEnd: { type: MarkerType.ArrowClosed, color: '#6b6e78', width: 12, height: 12 }
  }))
  return { nodes, jobEdges, dependencies }
}

function WorkflowNode({ id, data }) {
  const handlesVisible = data.isRelationshipFocus || data.isRelationshipRelated
  return (
    <div className={`workflow-flow-group ${data.isRelationshipFocus ? 'relationship-focus' : ''}`}>
      {(data.inputs || []).map((input, index) => <Handle key={`in-${input.port}`} id={`in-${input.port}`} type="target" position={Position.Left} className={`workflow-flow-handle ${handlesVisible ? 'visible' : ''}`} style={{ top: `${32 + index * 24}%`, background: input.color }} />)}
      <div className="workflow-flow-group-icon" aria-hidden="true">⌘</div>
      <div className="workflow-flow-group-copy"><span>Workflow</span><strong>{data.label}</strong><small>{data.description}</small></div>
      <div className="workflow-flow-expand-state">
        <span>{data.jobCount} jobs</span>
        <button className="nodrag nowheel" type="button" aria-label={`${data.expanded ? 'Collapse' : 'Expand'} ${data.label} jobs`} aria-expanded={data.expanded} onPointerDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); data.onToggle(id) }}>{data.expanded ? '−' : '+'}</button>
      </div>
      {(data.outputs || []).map((output, index) => <Handle key={`out-${output.port}`} id={`out-${output.port}`} type="source" position={Position.Right} className={`workflow-flow-handle ${handlesVisible ? 'visible' : ''}`} style={{ top: `${32 + index * 24}%`, background: output.color }} />)}
    </div>
  )
}

function JobNode({ data }) {
  return (
    <div className="workflow-flow-job" title={data.command || data.label}>
      <Handle id="job-in" type="target" position={Position.Top} className="workflow-flow-job-handle" />
      <span className="workflow-flow-job-dot" aria-hidden="true" />
      <div><small>{data.detail}</small><strong>{data.label}</strong></div>
      <span className="workflow-flow-job-state">Job</span>
      <Handle id="job-out" type="source" position={Position.Bottom} className="workflow-flow-job-handle" />
    </div>
  )
}

const nodeTypes = { workflow: WorkflowNode, job: JobNode }

export default function WorkflowFlow() {
  const [graph, setGraph] = useState(null)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [selectedWorkflow, setSelectedWorkflow] = useState('')
  const [showAllRelations, setShowAllRelations] = useState(true)
  const [expandedWorkflows, setExpandedWorkflows] = useState(() => new Set())
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    setError('')
    try {
      const payload = await api.orchestrationFlow()
      const nextGraph = buildGraph(payload)
      setGraph({ ...nextGraph, source: payload.source, generatedAt: payload.generatedAt })
      setNodes(nextGraph.nodes)
      setExpandedWorkflows(new Set())
      setSelectedWorkflow(current => nextGraph.nodes.some(node => node.id === current) ? current : '')
    } catch (loadError) {
      setError(loadError.message || String(loadError))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const relationshipEdges = useMemo(() => (graph?.dependencies || []).filter(dependency => showAllRelations || (selectedWorkflow && [dependency.PARENT_WORKFLOW_ID, dependency.CHILD_WORKFLOW_ID].map(String).includes(selectedWorkflow))).map(dependency => {
    const visual = relationVisual(dependency)
    return {
      id: `workflow-${dependency.DEPENDENCY_ID}`,
      source: String(dependency.PARENT_WORKFLOW_ID),
      target: String(dependency.CHILD_WORKFLOW_ID),
      sourceHandle: `out-${visual.port}`,
      targetHandle: `in-${visual.port}`,
      type: 'smoothstep',
      animated: false,
      markerEnd: { type: MarkerType.ArrowClosed, color: visual.color },
      style: { stroke: visual.color, strokeWidth: 3 }
    }
  }), [graph, selectedWorkflow, showAllRelations])

  const relatedIds = useMemo(() => new Set(relationshipEdges.flatMap(edge => [edge.source, edge.target])), [relationshipEdges])
  const visibleWorkflowIds = useMemo(() => {
    if (showAllRelations) return new Set(nodes.filter(node => node.type === 'workflow').map(node => node.id))
    const visible = new Set(relatedIds)
    if (selectedWorkflow) visible.add(selectedWorkflow)
    return visible
  }, [nodes, relatedIds, selectedWorkflow, showAllRelations])
  const displayedNodes = useMemo(() => nodes.map(node => {
    if (node.type === 'workflow') {
      const expanded = expandedWorkflows.has(node.id)
      return { ...node, hidden: !visibleWorkflowIds.has(node.id), style: { ...node.style, height: expanded ? node.data.expandedHeight : 86 }, data: { ...node.data, expanded, onToggle: toggleWorkflow, isRelationshipFocus: node.id === selectedWorkflow, isRelationshipRelated: relatedIds.has(node.id) } }
    }
    return { ...node, hidden: !visibleWorkflowIds.has(node.parentId) || !expandedWorkflows.has(node.parentId) }
  }), [nodes, selectedWorkflow, relatedIds, visibleWorkflowIds, expandedWorkflows])
  const selectedWorkflowLabel = nodes.find(node => node.id === selectedWorkflow)?.data?.label

  function toggleWorkflow(workflowId) {
    setExpandedWorkflows(current => {
      const next = new Set(current)
      if (next.has(workflowId)) next.delete(workflowId)
      else next.add(workflowId)
      return next
    })
  }

  function selectNode(node) {
    const workflowId = node.parentId || node.id
    setShowAllRelations(false)
    setSelectedWorkflow(workflowId)
  }

  return (
    <section className="page workflow-flow-page">
      <PageHeader breadcrumb="Orchestration / Flow" title="Flow" subtitle="Live workflow and job relationships from KUMO_TST.MONITOR_APP." actions={<button className="button" type="button" onClick={load} disabled={loading}>↻ Refresh</button>} />
      {error && <div className="alert error">{error}</div>}
      {loading && !graph && <LoadingState>Loading workflow graph…</LoadingState>}
      {graph && <>
        <div className="workflow-flow-toolbar" aria-label="Workflow relationship legend">
          <div className="workflow-flow-legend">
            <span><i className="success" /> Blue: trigger on success</span>
            <span><i className="warning" /> Yellow: trigger on warning</span>
            <span><i className="blocker" /> Red: blocker</span>
            <small>{graph.source} · {nodes.filter(node => node.type === 'workflow').length} workflows · {nodes.filter(node => node.type === 'job').length} jobs</small>
          </div>
          <div className={`workflow-flow-selection ${selectedWorkflow ? 'active' : ''}`}>
            <button type="button" className={`workflow-flow-show-all ${showAllRelations ? 'active' : ''}`} onClick={() => { setShowAllRelations(true); setSelectedWorkflow('') }}>{showAllRelations ? '✓ All relationships' : 'Show all relationships'}</button>
            <span>{showAllRelations ? 'Displaying every workflow relationship' : selectedWorkflow ? `Showing relationships for ${selectedWorkflowLabel}` : 'Click a workflow to show its relationships'}</span>
            {selectedWorkflow && <button type="button" onClick={() => { setSelectedWorkflow(''); setShowAllRelations(true) }}>Clear</button>}
          </div>
        </div>
        <div className="workflow-flow-canvas">
          {displayedNodes.length ? <ReactFlow key={graph.generatedAt || graph.source} nodes={displayedNodes} onNodesChange={onNodesChange} edges={[...(graph.jobEdges || []).filter(edge => visibleWorkflowIds.has(edge.data.workflowId) && expandedWorkflows.has(edge.data.workflowId)), ...relationshipEdges]} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.12 }} minZoom={0.2} maxZoom={1.6} nodesConnectable={false} elementsSelectable onNodeClick={(_, node) => selectNode(node)} onPaneClick={() => { setSelectedWorkflow(''); setShowAllRelations(true) }} proOptions={{ hideAttribution: true }}>
            <Background color="rgba(154, 156, 165, 0.18)" gap={24} size={1} />
            <MiniMap pannable zoomable nodeColor={node => node.type === 'workflow' ? '#4299ff' : '#444750'} maskColor="rgba(24, 26, 32, 0.72)" />
            <Controls showInteractive={false} />
          </ReactFlow> : <div className="soft-empty">No workflows were found in KUMO_TST.MONITOR_APP.</div>}
        </div>
        <p className="workflow-flow-hint">Click a workflow card to focus its relationships. Use +/− to expand or collapse its jobs. Workflow boxes can be moved freely.</p>
      </>}
    </section>
  )
}
