import { test as base, expect } from '@playwright/test'

export { expect }
export const test = base.extend({
  timeline: [async ({ page }, use) => {
    await page.clock.install({ time: new Date('2026-11-11T12:00:00Z') })
    const errors = [], requests = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => {window.copiedId=value}}}) })
    const wfId = '00000000-0000-0000-0000-000000000001', sql = '00000000-0000-0000-0000-000000000002', dbt = '00000000-0000-0000-0000-000000000003'
    const currentId = '00000000-0000-0000-0000-000000000004', oldId = '00000000-0000-0000-0000-000000000005', shortId = '00000000-0000-0000-0000-000000000006'
    const makeRun = (runId, start, end, status) => ({ workflowId: wfId, workflowName: 'PYMT LOAD', workflowGroup: 'LOAD', runId, executionId: runId, status, requestedAt: start, start, end, requestedBy: 'Timeline tester', mode: 'WORKFLOW', jobs: [] })
    const running = makeRun(currentId, '2026-11-11T11:55:00Z', null, 'RUNNING')
    const previous = makeRun(oldId, '2026-11-11T11:50:00Z', '2026-11-11T11:58:00Z', 'FAILED')
    const short = makeRun(shortId, '2026-11-11T11:59:00.100Z', '2026-11-11T11:59:00.225Z', 'SUCCESS')
    const child = { ...short, executionId: 'short-dbt', jobId: dbt, name: 'PYMT SDL', type: 'DBT', layer: 'SDL', position: 2 }
    short.jobs = [child]
    running.jobs = [{ ...running, executionId: 'sql-run', jobId: sql, name: 'PYMT READY', type: 'SQL', position: 1 }]
    previous.jobs = [{ ...previous, executionId: 'failed-dbt', jobId: dbt, name: 'PYMT SDL', type: 'DBT', position: 2, message: 'Example failure for validation' }]
    const wf = { WORKFLOW_ID: wfId, WORKFLOW_NAME: 'PYMT LOAD', WORKFLOW_GROUP: 'LOAD', IS_ENABLED: true, IS_SCHEDULE_ENABLED: true, SCHEDULE: '0 7 * * 1-5', SCHEDULE_TIMEZONE: 'Europe/Stockholm', run: running, lastRun: running, progress: { total: 2, succeeded: 0, failed: 0, skipped: 0 }, jobs: [
      { JOB_ID: sql, JOB_NAME: 'PYMT READY', JOB_TYPE: 'SQL', JOB_IS_ENABLED: true, JOB_SEQUENCE: 1, run: running.jobs[0], lastRun: running.jobs[0] },
      { JOB_ID: dbt, JOB_NAME: 'PYMT SDL', JOB_TYPE: 'DBT', JOB_IS_ENABLED: true, JOB_SEQUENCE: 2, run: { runId: currentId, executionId: 'pending-dbt', requestedAt: running.start, status: 'PENDING', position: 2, parentExecutionId: 'sql-run', selected: true }, lastRun: child }
    ] }
    const older = makeRun('00000000-0000-0000-0000-000000000007','2026-11-08T11:00:00Z','2026-11-08T11:10:00Z','SUCCESS')
    const monitor = { ok: true, rows: [wf], submissions: [], refreshSeconds: 5, generatedAt: '2026-11-11T12:00:00Z', runsEnabled: true }
    const definitions = [{JOB_ID:sql,JOB_NAME:'PYMT READY',JOB_TYPE:'SQL',IS_ENABLED:true,SQL_COMMAND:'SELECT 1'}, {JOB_ID:dbt,JOB_NAME:'PYMT SDL',JOB_TYPE:'DBT',JOB_LAYER:'SDL',IS_ENABLED:true,DBT_COMMAND:'run'}]
    const configuration={ok:true,source:'Validation',revision:'r1',workflows:[{...wf,ALLOW_MANUAL_TRIGGER:true}],jobs:definitions,assignments:definitions.map((job,index)=>({...job,WORKFLOW_ID:wfId,JOB_SEQUENCE:index+1,PARENT_WORKFLOW_ID:index?null:wfId,PARENT_JOB_ID:index?sql:null})),warnings:[],invalidWorkflows:[],rows:[{id:wfId,workflowId:wfId,jobId:null,name:'PYMT LOAD',parentId:null,allow:true,trigger:null,success:null,warning:null,error:null,sql:''}, ...definitions.map((job,index)=>({id:job.JOB_ID,jobId:job.JOB_ID,workflowId:wfId,name:job.JOB_NAME,parentId:index?sql:wfId,trigger:'DEP',allow:true,success:true,warning:true,error:false,sql:''}))]}
    const state = { failHistory: false, beforeHistory: null, historyRows: [short, running, previous, older] }
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname
      requests.push({ path, query: Object.fromEntries(url.searchParams), method: req.method() })
      let body = { ok: true }
      if (path === '/api/session') body = { ok: true, displayName: 'Tester' }
      if (path === '/api/health') body = { ok: true, legacyEngineRetired: true, environment: 'DEV' }
      if (path === '/api/orchestration/monitor') body = monitor
      if (path === '/api/orchestration/history') {
        if (state.beforeHistory) await state.beforeHistory(url)
        if (state.failHistory) { state.failHistory = false; return route.fulfill({ status: 503, json: { error: 'History temporarily unavailable' } }) }
        let runs = state.historyRows
        const from = Date.parse(url.searchParams.get('start')), to = Date.parse(url.searchParams.get('end'))
        runs = runs.filter(run => (!Number.isFinite(to) || Date.parse(run.start)<=to) && (!Number.isFinite(from) || Date.parse(run.end || '2026-11-11T12:00:00Z')>=from))
        const cursor = url.searchParams.get('cursor')
        if (cursor) runs = runs.slice(runs.findIndex(run => run.runId === cursor) + 1)
        const more = runs.length > 3
        runs = runs.slice(0, 3)
        body = { ok: true, runs, nextCursor: more ? runs.at(-1).runId : null, generatedAt: '2026-11-11T12:00:00Z' }
      }
      if (path.endsWith('/log')) body = { ok: true, history: { RUN_ID: 'short-dbt', WORKFLOW_NAME: 'PYMT SDL', WORKFLOW_TYPE: 'DBT', STATUS: 'SUCCESS' }, jobExecutions: [], modelProgress: [], testProgress: [], runLog: [], warnings: {} }
      if (path === '/api/orchestration/configuration') body=configuration
      if (path === '/api/orchestration/engine') body={ok:true,installed:true,runsEnabled:true,preparedWorkflows:1}
      if (path.endsWith('/dag-preview')) body={ok:true,nodes:[],edges:[],modelCount:2,testCount:1}
      if (path.endsWith('/dag')) body = { ok: true, kind: url.searchParams.get('jobId') ? 'models' : 'jobs', run: { RUN_ID: shortId, STATUS: 'SUCCESS' }, nodes: [], edges: [], errors: [], tests: [] }
      return route.fulfill({ json: body })
    })
    await use({ wfId, sql, dbt, currentId, shortId, older, monitor, makeRun, requests, state })
    expect(errors).toEqual([])
    expect(requests.filter(req => req.method !== 'GET' && !req.path.endsWith('/dag-preview'))).toEqual([])
  }, { auto: true }]
})
