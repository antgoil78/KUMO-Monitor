import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, useNodesState } from '@xyflow/react'

import dagre from '@dagrejs/dagre'
import { api } from '../api.js'
import ConfigurationSource, { useConfigurationSource } from '../components/ConfigurationSource.jsx'
import LoadingState from '../components/LoadingState.jsx'
import PageHeader from '../components/PageHeader.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import useOrchestrationMonitor, { activeWorkflowStatuses, applySubmission } from '../utils/useOrchestrationMonitor.js'
import { dagExecution, dagTone, dependencyScope } from '../utils/workflowDag.js'
import { RunWorkflowDialog } from './WorkflowExecution.jsx'
import './WorkflowFlow.css'

const WORKFLOW_WIDTH = 340
const JOB_HEIGHT = 76
const JOB_GAP = 12
const workflowColors = { running: '#69afff', success: '#53d397', warning: '#f2c94c', failed: '#ff787c', waiting: '#c0adf5', neutral: '#787b84' }

function triggerList(value) {
  if (Array.isArray(value)) return value
  return String(value || '').match(/[A-Z][A-Z0-9_]*/gi) || []
}

function relationVisual(dependency) {
  const type = String(dependency.DEPENDENCY_TYPE || 'TRIGGER').toUpperCase()
  const status = String(dependency.MIN_STATUS_LEVEL || 'SUCCESS').toUpperCase()
  if (dependency.ALLOW === false) return { color: '#ef5350', port: 'red', label: 'Execution not allowed' }
  if (type === 'BLOCKER') return { color: '#ef5350', port: 'red', label: `Blocks on ${status.toLowerCase()}` }
  if (['SUCCESS', 'WARNING', 'ERROR'].some(key => Object.hasOwn(dependency, key))) {
    const allowed = ['SUCCESS', 'WARNING', 'ERROR'].filter(key => dependency[key] !== false)
    const label = allowed.length === 3 ? 'On any completed status' : allowed.length ? `On ${allowed.map(value => value.toLowerCase()).join(' / ')}` : 'All statuses rejected'
    const tone = !allowed.includes('SUCCESS') && allowed.includes('ERROR') ? { color: '#ef5350', port: 'red' }
      : allowed.includes('WARNING') && !allowed.includes('ERROR') ? { color: '#f2c94c', port: 'yellow' }
      : { color: '#4299ff', port: 'blue' }
    return { ...tone, label: `${label}${dependency.SQL_CONDITION ? ' + SQL condition' : ''}` }
  }
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

function buildGraph(payload, expandedWorkflows) {
  const workflows = payload.workflows || []
  const definitions = payload.jobs || []
  const workflowIds = new Set(workflows.map(row => String(row.WORKFLOW_ID)))
  const dependencies = (payload.dependencies || []).filter(row => workflowIds.has(String(row.PARENT_WORKFLOW_ID)) && workflowIds.has(String(row.CHILD_WORKFLOW_ID)))
  const inputs = new Map(), outputs = new Map()
  for (const dependency of dependencies) {
    const visual = relationVisual(dependency)
    const source = String(dependency.PARENT_WORKFLOW_ID), target = String(dependency.CHILD_WORKFLOW_ID)
    if (!outputs.has(source)) outputs.set(source, new Map())
    if (!inputs.has(target)) inputs.set(target, new Map())
    outputs.get(source).set(visual.port, visual)
    inputs.get(target).set(visual.port, visual)
  }
  const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
  layout.setGraph({ rankdir: 'LR', ranksep: 160, nodesep: 40, marginx: 30, marginy: 30 })
  const nodes = []
  for (const workflow of workflows) {
    const workflowId = String(workflow.WORKFLOW_ID)
    const jobs = orderedJobs(definitions.filter(row => String(row.WORKFLOW_ID) === workflowId))
    const expanded = expandedWorkflows.has(workflowId)
    const jobStart = 158
    const height = expanded ? Math.max(jobStart + 50, jobStart + jobs.length * (JOB_HEIGHT + JOB_GAP) + 12) : 152
    const triggers = triggerList(workflow.EXTERNAL_TRIGGERS)
    layout.setNode(workflowId, { width: WORKFLOW_WIDTH, height })
    nodes.push({ id: workflowId, type: 'workflow', position: { x: 0, y: 0 },
      data: { label: workflow.WORKFLOW_NAME, description: workflow.WORKFLOW_GROUP || (triggers.length ? triggers.join(' · ') : 'Manual / dependency'),
        jobCount: jobs.length, expanded, inputs: [...(inputs.get(workflowId)?.values() || [])], outputs: [...(outputs.get(workflowId)?.values() || [])] },
      style: { width: WORKFLOW_WIDTH, height } })
    if (expanded) jobs.forEach((job, index) => {
      const parent = jobs.find(parent => String(parent.JOB_ID) === String(job.PARENT_JOB_ID || ''))
      nodes.push({ id: `${workflowId}::${job.JOB_ID}`, type: 'job', parentId: workflowId, extent: 'parent',
        position: { x: 24, y: jobStart + index * (JOB_HEIGHT + JOB_GAP) },
        data: { jobId: String(job.JOB_ID), label: job.JOB_NAME,
          command: job.JOB_TYPE === 'DBT' ? job.DBT_COMMAND : job.SQL_COMMAND || job.COMMAND_LINE,
          detail: `${job.JOB_TYPE || 'Job'} · ${workflow.JOB_EXECUTION_MODE === 'PARALLEL' ? 'Parallel job' : parent ? `After ${parent.JOB_NAME}` : 'Root job'}` },
        draggable: false })
    })
  }
  for (const dependency of dependencies) layout.setEdge(String(dependency.PARENT_WORKFLOW_ID), String(dependency.CHILD_WORKFLOW_ID))
  dagre.layout(layout)
  for (const node of nodes.filter(node => node.type === 'workflow')) {
    const position = layout.node(node.id)
    node.position = { x: position.x - WORKFLOW_WIDTH / 2, y: position.y - node.style.height / 2 }
  }
  const nodeIds = new Set(nodes.map(node => node.id))
  const parallelIds = new Set(workflows.filter(row => row.JOB_EXECUTION_MODE === 'PARALLEL').map(row => String(row.WORKFLOW_ID)))
  const jobEdges = definitions.filter(row => row.PARENT_JOB_ID && !parallelIds.has(String(row.WORKFLOW_ID)) &&
    nodeIds.has(`${row.WORKFLOW_ID}::${row.PARENT_JOB_ID}`) && nodeIds.has(`${row.WORKFLOW_ID}::${row.JOB_ID}`)).map(row => ({
    id: `job-${row.WORKFLOW_ID}-${row.PARENT_JOB_ID}-${row.JOB_ID}`,
    source: `${row.WORKFLOW_ID}::${row.PARENT_JOB_ID}`, target: `${row.WORKFLOW_ID}::${row.JOB_ID}`,
    type: 'smoothstep', className: 'workflow-flow-job-edge',
    style: { stroke: '#6b6e78', strokeWidth: 1.4 },
    markerEnd: { type: MarkerType.ArrowClosed, color: '#6b6e78', width: 12, height: 12 } }))
  return { nodes, jobEdges, dependencies }
}

function WorkflowNode({ id, data }) {
  const handlesVisible = data.isRelationshipFocus || data.isRelationshipRelated
  return (
    <div className={`workflow-flow-group tone-${data.tone} ${data.isRelationshipFocus ? 'relationship-focus' : ''}`}>
      {(data.inputs || []).map((input, index) => <Handle key={`in-${input.port}`} id={`in-${input.port}`} type="target" position={Position.Left} className={`workflow-flow-handle ${handlesVisible ? 'visible' : ''}`} style={{ top: `${32 + index * 24}%`, background: input.color }} />)}
      <div className="workflow-flow-group-icon" aria-hidden="true">⌘</div>
      <div className="workflow-flow-group-copy"><span>Workflow</span><strong>{data.label}</strong><small>{data.description}</small></div>
      <div className="workflow-flow-expand-state">
        <span>{data.jobCount} jobs</span>
        <button className="nodrag nowheel" type="button" aria-label={`${data.expanded ? 'Collapse' : 'Expand'} ${data.label} jobs`} aria-expanded={data.expanded} onPointerDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); data.onToggle(id) }}>{data.expanded ? '−' : '+'}</button>
      </div>
      <ExecutionSummary data={data} />
      <div className="workflow-flow-run-row">
        <button className="button nodrag nowheel" type="button" aria-label={`Run ${data.label}`} disabled={Boolean(data.runUnavailable)} title={data.runUnavailable || `Start ${data.label}`} onPointerDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); data.onRun(id) }}>▶ Run</button>
        <small title={data.waitDetails || data.runUnavailable}>{data.waitDetails || data.runUnavailable}</small>
      </div>
      {(data.outputs || []).map((output, index) => <Handle key={`out-${output.port}`} id={`out-${output.port}`} type="source" position={Position.Right} className={`workflow-flow-handle ${handlesVisible ? 'visible' : ''}`} style={{ top: `${32 + index * 24}%`, background: output.color }} />)}
    </div>
  )
}

function ExecutionSummary({ data }) {
  return <div className="workflow-flow-execution" title={data.waitDetails}>
    <div aria-label="Last result"><small>Last result</small><StatusBadge status={data.result} showIcon={false} /></div>
    <div aria-label="State"><small>State</small><span><StatusBadge status={data.state} count={data.state === 'WAITING' ? data.waitingCount : undefined} showIcon={false} />{data.waitingCount > 0 && data.state !== 'WAITING' && <StatusBadge status="WAITING" count={data.waitingCount} showIcon={false} />}</span></div>
  </div>
}

function JobNode({ data }) {
  return (
    <div className="workflow-flow-job" title={data.command || data.label}>
      <Handle id="job-in" type="target" position={Position.Top} className="workflow-flow-job-handle" />
      <span className="workflow-flow-job-dot" aria-hidden="true" />
      <div><small>{data.detail}</small><strong>{data.label}</strong></div>
      <ExecutionSummary data={data} />
      <Handle id="job-out" type="source" position={Position.Bottom} className="workflow-flow-job-handle" />
    </div>
  )
}

const nodeTypes = { workflow: WorkflowNode, job: JobNode }

export default function WorkflowFlow() {
  const selection = useConfigurationSource()
  const { source } = selection
  const [graph, setGraph] = useState(null)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [selectedWorkflow, setSelectedWorkflow] = useState('')
  const flowInstance = useRef(null)
  const [centerRequest, setCenterRequest] = useState(null)
  const [expandedWorkflows, setExpandedWorkflows] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('')
  const live = useOrchestrationMonitor(Boolean(source), source)
  const [runSelection, setRunSelection] = useState('')
  const [submittingWorkflow, setSubmittingWorkflow] = useState('')
  const catalogReload = useRef('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const loadVersion = useRef(0)

  async function load() {
    if (!source) return
    const version = ++loadVersion.current
    setLoading(true); setError('')
    try {
      const payload = await (source === 'POSTGRES' ? api.postgresFlow() : api.orchestrationFlow())
      if (version !== loadVersion.current) return
      setGraph(payload)
      setExpandedWorkflows(current => new Set([...current].filter(id => payload.workflows.some(row => String(row.WORKFLOW_ID) === id))))
      setSelectedWorkflow(current => payload.workflows.some(row => String(row.WORKFLOW_ID) === current) ? current : '')
    } catch (failure) { if (version === loadVersion.current) setError(failure.message) }
    finally { if (version === loadVersion.current) setLoading(false) }
  }
  useEffect(() => {
    setGraph(null); setSelectedWorkflow(''); setCenterRequest(null); setRunSelection(''); catalogReload.current = ''
    load()
    return () => { loadVersion.current++ }
  }, [source])

  useEffect(() => {
    if (!graph || !live.data?.configurationRevision || live.data.configurationRevision === graph.configurationRevision) return
    const key = `${source}:${live.data.configurationRevision}`
    if (catalogReload.current !== key) { catalogReload.current = key; load() }
  }, [source, graph, live.data?.configurationRevision])
  useEffect(() => { if (!live.refreshing) setSubmittingWorkflow('') }, [live.data, live.refreshing])
  const liveRows = useMemo(() => new Map((live.data?.rows || []).map(row => [String(row.WORKFLOW_ID), applySubmission(row, live.data.submissions)])), [live.data])
  const liveUnavailable = live.error ? 'Live status is unavailable. Waiting for reconnection.'
    : !live.data || live.loading ? 'Loading live status…'
      : !live.data.runsEnabled ? 'Workflow execution is disabled.'
        : source === 'POSTGRES' && live.data.worker?.state === 'UNAVAILABLE' ? 'Execution worker is unavailable.'
          : graph && live.data.configurationRevision !== graph.configurationRevision ? 'Updating workflow configuration…' : ''
  function runnableWorkflow(id) {
    const row = liveRows.get(id)
    if (!row) return null
    const reason = liveUnavailable || (submittingWorkflow === id ? 'Execution request accepted. Refreshing status…' : '') ||
      (!row.canRun || activeWorkflowStatuses.has(row.run?.status) ? row.runUnavailable || 'Workflow is already queued or running.' : '')
    return { ...row, canRun: !reason, runUnavailable: reason }
  }
  const groups = useMemo(() => [...new Set((graph?.workflows || []).map(w => w.WORKFLOW_GROUP || 'Ungrouped'))].sort(), [graph])
  const selectedWorkflowLabel = graph?.workflows.find(w => String(w.WORKFLOW_ID) === selectedWorkflow)?.WORKFLOW_NAME
  const scopeKey = useMemo(() => {
    if (!graph) return '[]'
    const search = query.trim().toLowerCase()
    const seeds = selectedWorkflow ? [selectedWorkflow] : graph.workflows.filter(w =>
      (!group || (w.WORKFLOW_GROUP || 'Ungrouped') === group) &&
      (!search || [w.WORKFLOW_NAME, w.WORKFLOW_GROUP].some(value => String(value || '').toLowerCase().includes(search))))
      .map(w => String(w.WORKFLOW_ID))
    return JSON.stringify([...dependencyScope(graph.dependencies, seeds)].sort())
  }, [graph, selectedWorkflow, query, group])
  const displayedPayload = useMemo(() => {
    if (!graph) return { workflows: [], jobs: [], dependencies: [] }
    const ids = new Set(JSON.parse(scopeKey))
    return {
      workflows: graph.workflows.filter(w => ids.has(String(w.WORKFLOW_ID))),
      jobs: graph.jobs.filter(j => ids.has(String(j.WORKFLOW_ID))),
      dependencies: graph.dependencies.filter(edge => ids.has(String(edge.PARENT_WORKFLOW_ID)) && ids.has(String(edge.CHILD_WORKFLOW_ID)))
    }
  }, [graph, scopeKey])
  const layout = useMemo(() => buildGraph(displayedPayload, expandedWorkflows), [displayedPayload, expandedWorkflows])
  useLayoutEffect(() => { setNodes(layout.nodes) }, [layout, setNodes])
  useEffect(() => {
    if (!centerRequest) return undefined
    // Wait for a changed dependency scope to reach ReactFlow, then use the
    // current card position (including any manual dragging) at the same zoom.
    const frame = window.requestAnimationFrame(() => {
      const flow = flowInstance.current
      const node = flow?.getNode(centerRequest.id)
      if (node) flow.setCenter(node.position.x + node.style.width / 2, node.position.y + node.style.height / 2,
        { zoom: flow.getZoom(), duration: 200, interpolate: 'linear' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [centerRequest])
  const relationshipEdges = useMemo(() => layout.dependencies.map(dependency => {
    const visual = relationVisual(dependency)
    return { id: `workflow-${dependency.DEPENDENCY_ID}`, source: String(dependency.PARENT_WORKFLOW_ID), target: String(dependency.CHILD_WORKFLOW_ID),
      sourceHandle: `out-${visual.port}`, targetHandle: `in-${visual.port}`, type: 'smoothstep', label: visual.label,
      labelStyle: { fill: '#b8bac0', fontSize: 10 }, labelBgStyle: { fill: '#181a20' },
      markerEnd: { type: MarkerType.ArrowClosed, color: visual.color }, style: { stroke: visual.color, strokeWidth: 2 } }
  }), [layout])
  const relatedIds = useMemo(() => new Set(relationshipEdges.flatMap(edge => [edge.source, edge.target])), [relationshipEdges])
  const displayedNodes = nodes.map(node => {
    const workflow = runnableWorkflow(node.parentId || node.id)
    const job = node.type === 'job' ? workflow?.jobs.find(job => String(job.JOB_ID) === node.data.jobId) : undefined
    const waits = workflow?.planning?.waitingFor || []
    const parentStates = new Map()
    for (const item of waits.filter(item => item.relationship === 'PARENT')) {
      const states = parentStates.get(item.name) || new Set()
      states.add(item.status === 'STALE_PARENT' ? 'STALE' : item.status === 'ADMITTED' ? 'QUEUED' : item.status || 'MISSING')
      parentStates.set(item.name, states)
    }
    const waitDetails = [...parentStates].map(([name, states]) => `Parent ${name} (${[...states].join(' / ')})`).join(' · ') ||
      (!waits.length ? workflow?.planning?.message || '' : '')
    const execution = dagExecution(workflow, job)
    return { ...node, data: { ...node.data, ...execution, tone: dagTone(execution), waitDetails,
      onToggle: toggleWorkflow, onRun: setRunSelection, runUnavailable: workflow?.runUnavailable || (!workflow ? 'Live workflow status is unavailable.' : ''),
      isRelationshipFocus: node.id === selectedWorkflow, isRelationshipRelated: relatedIds.has(node.id) } }
  })
  const liveEdges = relationshipEdges.map(edge => ({ ...edge, animated: liveRows.get(edge.target)?.planning?.status === 'RUNNING' }))
  const dialogWorkflow = runSelection && runnableWorkflow(runSelection)
  // Fit when opening or filtering the view. Clicking pans at the current zoom;
  // job expansion and live updates leave the viewport alone.
  const viewKey = JSON.stringify([source, query, group])

  function toggleWorkflow(id) {
    setExpandedWorkflows(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })
  }
  function changeFilter(setValue, value) { setValue(value); setSelectedWorkflow(''); setCenterRequest(null) }
  function selectWorkflow(_, node) {
    const id = node.parentId || node.id
    setSelectedWorkflow(id)
    setCenterRequest({ id })
  }

  return <section className="page workflow-flow-page">
    <PageHeader breadcrumb="Workflow / DAG View" title="DAG View" subtitle={`Live workflow dependencies from ${graph?.source || 'the configured orchestration catalog'}.`} actions={<button className="button" type="button" onClick={() => { load(); live.refresh() }} disabled={loading || !source}>↻ Refresh</button>} />
    <ConfigurationSource selection={selection} revision={graph?.configurationRevision} />
    {error && <div className="alert error" role="alert">{error}</div>}
    {live.error && <div className="alert error" role="alert">Live updates interrupted: {live.error}. Reconnecting automatically. <button className="button" type="button" onClick={live.refresh}>Retry live status</button></div>}
    {loading && !graph && <LoadingState>Loading workflow DAG…</LoadingState>}
    {graph && <>
      <div className="workflow-flow-live" role="status">
        <span className={live.error || live.loading ? 'unavailable' : 'connected'}>{live.error ? 'Reconnecting…' : live.loading ? 'Connecting…' : '● Live'}</span>
        <span>{source === 'POSTGRES' ? 'PostgreSQL · refreshes every 2 seconds' : 'Shared monitor status feed'}{live.data?.worker && ` · Worker ${live.data.worker.state.toLowerCase()}`}</span>
        {live.data?.generatedAt && <span>Updated {new Date(live.data.generatedAt).toLocaleTimeString()}</span>}
      </div>
      <div className="workflow-flow-filters">
        <input className="search-input" type="search" aria-label="Filter workflows by name or group" placeholder="Filter by workflow name or group…" value={query} onChange={event => changeFilter(setQuery, event.target.value)} />
        <select className="status-select" aria-label="Filter workflow group" value={group} onChange={event => changeFilter(setGroup, event.target.value)}><option value="">All groups</option>{groups.map(name => <option key={name}>{name}</option>)}</select>
        {(query || group) && <button className="button" type="button" onClick={() => { setQuery(''); setGroup(''); setSelectedWorkflow(''); setCenterRequest(null) }}>Clear filters</button>}
      </div>
      <div className="workflow-flow-toolbar">
        <div className="workflow-flow-legends">
          <div className="workflow-flow-status-legend" aria-label="Workflow colors"><small>Workflows</small><span><i className="success" /> Success</span><span><i className="warning" /> Warning</span><span><i className="failed" /> Error</span><span><i className="running" /> Running</span><span><i className="waiting" /> Waiting / queued</span></div>
          <div className="workflow-flow-legend" aria-label="Dependency conditions"><small>Connections</small><span><i className="success" /> Success</span><span><i className="warning" /> Warning</span><span><i className="blocker" /> Failure / blocked</span></div>
        </div>
        <div className="workflow-flow-selection" aria-live="polite">
          <span>{selectedWorkflow ? `Dependencies: ${selectedWorkflowLabel}` : 'Workflow overview'} · {displayedPayload.workflows.length} of {graph.workflows.length} workflows</span>
          {selectedWorkflow && <button type="button" onClick={() => { setSelectedWorkflow(''); setCenterRequest(null) }}>Back to overview</button>}
        </div>
      </div>
      <div className="workflow-flow-canvas">
        {displayedNodes.length ? <ReactFlow key={viewKey} onInit={instance => { flowInstance.current = instance }} nodes={displayedNodes} onNodesChange={onNodesChange} edges={[...layout.jobEdges, ...liveEdges]} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.12 }} minZoom={0.08} maxZoom={1.6} nodesConnectable={false} elementsSelectable onNodeClick={selectWorkflow} proOptions={{ hideAttribution: true }}>
          <Background color="rgba(154, 156, 165, 0.18)" gap={24} size={1} />
          <MiniMap pannable zoomable nodeColor={node => node.type === 'workflow' ? workflowColors[node.data.tone] || workflowColors.neutral : '#444750'} maskColor="rgba(24, 26, 32, 0.72)" />
          <Controls showInteractive={false} />
        </ReactFlow> : <div className="soft-empty">No workflows match the current filters.</div>}
      </div>
      <p className="workflow-flow-hint">Search or click a workflow to show its connected dependency group, including upstream, downstream and parallel branches. Clicking centers the workflow and keeps your zoom. Blue means running now; green, yellow and red show the last completed result. Waiting requests use purple. Use Run to review the execution scope, and +/− for live job status.</p>
    </>}
    {dialogWorkflow && <RunWorkflowDialog key={runSelection} workflow={dialogWorkflow} jobs={dialogWorkflow.jobs.filter(job => !job.runOnly)} onClose={() => setRunSelection('')} onAccepted={receipt => {
      live.acceptSubmission(receipt); setSubmittingWorkflow(runSelection); setRunSelection(''); live.refresh()
    }} />}
  </section>
}
