import { useEffect, useState } from 'react'
import { api } from '../api.js'
import './WorkflowRunPlan.css'

function parentCondition(parent) {
  if (!parent.allow) return 'Blocked by dependency rule'
  const ignoreWarnings = parent.warning === null && (parent.success !== false || parent.error !== false)
  const outcomes = [['success', 'Success'], ['warning', 'Warning'], ['error', 'Fail']]
    .filter(([flag]) => flag !== 'warning' || !ignoreWarnings)
    .filter(([flag]) => parent[flag] !== false).map(([, label]) => label)
  return `${outcomes.length ? `On parent ${outcomes.join(' or ')}` : 'No outcome is allowed'}${ignoreWarnings ? ', ignore warnings' : ''}`
}

function conditionSummary(parents) {
  if (!parents.length) return ''
  const conditions = [...new Set(parents.map(parentCondition))]
  const name = parent => `${parent.name}${parent.inPlan ? '' : ' (existing result)'}`
  if (conditions.length === 1) return conditions[0] + (parents.length > 1 || !parents[0].inPlan ? ` · ${parents.map(name).join(' + ')}` : '')
  return parents.map(parent => `${name(parent)}: ${parentCondition(parent)}`).join('; ')
}

export default function WorkflowRunPlan({ plan }) {
  const byKey = new Map(plan.nodes.map(node => [node.key, node]))
  const children = new Map(plan.nodes.map(node => [node.key, []]))
  const levels = new Map()
  function level(key) {
    if (!levels.has(key)) {
      const parents = byKey.get(key).parents.filter(parent => parent.inPlan && byKey.has(parent.key))
      levels.set(key, parents.length ? 1 + Math.max(...parents.map(parent => level(parent.key))) : 0)
    }
    return levels.get(key)
  }
  const compare = (a, b) => byKey.get(a).name.localeCompare(byKey.get(b).name, 'en', { numeric: true })
  // Display a join once, under its last/deepest parent. Every prerequisite
  // remains visible in the row label, including parents outside this run.
  const roots = []
  for (const node of plan.nodes) {
    const parents = node.parents.filter(parent => parent.inPlan && byKey.has(parent.key))
      .map(parent => parent.key).sort((a, b) => level(a) - level(b) || compare(a, b))
    if (parents.length) children.get(parents.at(-1)).push(node.key)
    else roots.push(node.key)
  }
  for (const keys of children.values()) keys.sort(compare)
  function branch(key) {
    const node = byKey.get(key)
    const descendants = children.get(key) || []
    return <li key={key}>
      <details className={`run-plan-node${key === plan.rootKey ? ' run-plan-entry' : ''}`}>
        <summary className="run-plan-heading"><strong>{node.name}</strong>{node.parents.length > 0 && <span className="run-plan-condition"> — {conditionSummary(node.parents)}</span>}</summary>
        <div className="run-plan-details">
          {node.parents.length > 0 && <ul className="run-plan-parents" aria-label={`Parents of ${node.name}`}>{node.parents.map(parent => <li key={parent.key}>
            <strong>{parent.name}</strong>{!parent.inPlan && <span className="run-plan-external">Existing result · not rerun</span>}
            <small>{parentCondition(parent)}{parent.parentMustBeNewer && ' · parent must finish after this workflow’s previous start'}</small>
          </li>)}</ul>}
          <details className="run-plan-jobs"><summary>{node.jobs.length} {node.jobs.length === 1 ? 'job' : 'jobs'} · {node.mode === 'PARALLEL' ? 'Parallel' : 'Sequential'}</summary>
            <ol>{node.jobs.map(job => <li key={job.key}><strong>{job.name}</strong><span>{job.type} · {job.layer}</span></li>)}</ol>
          </details>
        </div>
      </details>
      {node.issues.length > 0 && <p className="run-plan-issue">{node.issues.join('; ')}</p>}
      {descendants.length > 0 && <ul className="run-plan-branches">{descendants.map(branch)}</ul>}
    </li>
  }
  return <section className="run-plan" aria-label="Execution plan">
    <div className="run-plan-title"><h3>Execution plan</h3><span>{plan.nodes.length} {plan.nodes.length === 1 ? 'workflow' : 'workflows'}</span></div>
    <p className="form-help">Each workflow runs once. All listed parents must permit execution. Expand a workflow for details.</p>
    <ul className="run-plan-tree">{roots.sort((a, b) => a === plan.rootKey ? -1 : b === plan.rootKey ? 1 : compare(a, b)).map(branch)}</ul>
    {plan.omitted.length > 0 && <p className="run-plan-omitted">Disabled workflows omitted: {plan.omitted.map(node => node.name).join(', ')}. Their enabled ancestors still apply.</p>}
    <p className="form-help">Ancestors of the starting workflow are not rerun. Starting rechecks the configuration and active runs.</p>
  </section>
}

export function useWorkflowRunPlan(workflow, includeChildren) {
  const [result, setResult] = useState(null)
  const [retry, setRetry] = useState(0)
  const postgres = workflow.source === 'POSTGRES'
  const key = `${workflow.graphId}:${workflow.WORKFLOW_ID}:${includeChildren}:${workflow.configurationRevision}:${retry}`
  useEffect(() => {
    if (!postgres) return undefined
    let active = true
    api.workflowRunPlan(workflow.graphId, workflow.WORKFLOW_ID, includeChildren)
      .then(plan => { if (active) setResult({ key, plan }) })
      .catch(error => { if (active) setResult({ key, error: error.message }) })
    return () => { active = false }
  }, [postgres, key, workflow.graphId, workflow.WORKFLOW_ID, includeChildren])
  const current = result?.key === key ? result : null
  return { plan: current?.plan, error: current?.error, loading: postgres && !current, retry: () => setRetry(value => value + 1) }
}
