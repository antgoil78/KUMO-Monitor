"""Explicit, targeted read-only import of WF1–WF4. Never invokes the old engine."""
from datetime import datetime, timezone

from .dependencies import graph_definition

NAMES = ('WF1','WF2','WF3','WF4')


def read_source():
    import snowflake_client as sf
    from snowflake.connector import DictCursor
    with sf.connection(use_warehouse=True, include_context=False, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT WORKFLOW_ID,WORKFLOW_NAME,WORKFLOW_GROUP,IS_ENABLED,IS_SCHEDULE_ENABLED,WAREHOUSE_NAME,JOB_EXECUTION_MODE FROM KUMO_DEV.MONITOR_META.WORKFLOW WHERE WORKFLOW_NAME IN ('WF1','WF2','WF3','WF4') ORDER BY WORKFLOW_NAME")
            workflows = cur.fetchall()
            if len(workflows) != 4:
                raise ValueError('Expected exactly WF1–WF4 in DEV')
            ids = [w['WORKFLOW_ID'] for w in workflows]
            cur.execute('SELECT WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW,PARENT_MUST_BE_NEWER FROM KUMO_DEV.MONITOR_META.DEPENDENCIES WHERE WORKFLOW_ID IN (%s,%s,%s,%s) ORDER BY WORKFLOW_ID,JOB_ID', ids)
            rules = cur.fetchall(); jobs = sorted({r['JOB_ID'] for r in rules if r['JOB_ID']})
            if not jobs:
                raise ValueError('No SQL jobs are assigned to WF1–WF4')
            cur.execute('SELECT JOB_ID,JOB_NAME,JOB_TYPE,IS_ENABLED,SQL_COMMAND,TIMEOUT_SECONDS FROM KUMO_DEV.MONITOR_META.JOB WHERE JOB_ID IN ('+','.join(['%s']*len(jobs))+') ORDER BY JOB_NAME', jobs)
            return dict(workflows=workflows, rules=rules, jobs=cur.fetchall())
        finally:
            cur.close()


def snapshot(data):
    workflows, rules, jobs = data['workflows'], data['rules'], data['jobs']
    by_name = {w['WORKFLOW_NAME']:w for w in workflows}
    if len(workflows)!=4 or set(by_name)!=set(NAMES) or any(w['WORKFLOW_GROUP']!='DEPENDENCY TEST' for w in workflows):
        raise ValueError('Import is restricted to the four existing DEV dependency-test workflows')
    by_id = {w['WORKFLOW_ID']:w['WORKFLOW_NAME'] for w in workflows}
    by_job = {j['JOB_ID']:j for j in jobs}
    nodes = []
    for name in NAMES:
        wf = by_name[name]; members = [r for r in rules if r['WORKFLOW_ID']==wf['WORKFLOW_ID']]
        headers = [r for r in members if not r['JOB_ID']]
        assignments = [r for r in members if r['JOB_ID']]
        if not headers or not assignments or len({r['JOB_ID'] for r in assignments})!=len(assignments):
            raise ValueError('Missing or duplicate workflow/job assignments for '+name)
        parents = []; roots = [r for r in headers if r['PARENT_ID'] is None]
        if len(roots)>1 or (roots and len(headers)>1):
            raise ValueError('Mixed root and dependency rules for '+name)
        for rule in headers:
            if rule.get('SQL') or (rule['PARENT_ID'] and rule['TRIGGER_TYPE']!='DEP') or (not rule['PARENT_ID'] and rule['TRIGGER_TYPE'] is not None):
                raise ValueError('SQL conditions or mixed trigger policies need a later migration; '+name+' was not imported')
            if rule['PARENT_ID']:
                if rule['PARENT_ID'] not in by_id:
                    raise ValueError('This WF1–WF4 snapshot has a parent outside the selected trial')
                parents.append(dict(key=by_id[rule['PARENT_ID']], success=rule['SUCCESS'], warning=rule['WARNING'], error=rule['ERROR'],
                                    allow=rule['ALLOW'] is not False, parentMustBeNewer=bool(rule['PARENT_MUST_BE_NEWER'])))
        ordered = []; parent = wf['WORKFLOW_ID']; remaining = list(assignments)
        while remaining:
            candidates = [r for r in remaining if r['PARENT_ID']==parent]
            if len(candidates)!=1:
                raise ValueError('Job assignment order is branching, missing or cyclic for '+name)
            rule = candidates[0]; job = by_job.get(rule['JOB_ID'])
            if (not job or job['JOB_TYPE']!='SQL' or not job['IS_ENABLED'] or rule['ALLOW'] is False or rule.get('SQL')
                    or any(rule.get(k) is not None for k in ('SUCCESS','WARNING','ERROR'))):
                raise ValueError('Job is disabled or has an unsupported command/condition/result gate for '+name)
            ordered.append(dict(key=job['JOB_ID'], sql=job['SQL_COMMAND'], timeoutSeconds=int(job.get('TIMEOUT_SECONDS') or 60)))
            remaining.remove(rule); parent = rule['JOB_ID']
        nodes.append(dict(key=name, name=name, enabled=bool(wf['IS_ENABLED']), allow=not roots or roots[0]['ALLOW'] is not False,
                          plan=dict(mode=wf.get('JOB_EXECUTION_MODE') or 'SEQUENTIAL', jobs=ordered), parents=parents))
    body = dict(key='wf1-sql-trial', name='WF1 SQL dependency trial', enabled=True, nodes=nodes)
    validated = graph_definition(body)
    expected = {'WF1':set(),'WF2':{'WF1'},'WF3':{'WF1'},'WF4':{'WF2','WF3'}}
    if any({p['key'] for p in n['parents']}!=expected[n['key']] for n in validated['nodes']):
        raise ValueError('WF1–WF4 no longer form the expected diamond; review before importing')
    source = dict(namespace='KUMO_DEV.MONITOR_META', workflows={n:by_name[n]['WORKFLOW_ID'] for n in NAMES},
                  jobs={n:[r['JOB_ID'] for r in rules if r['WORKFLOW_ID']==by_name[n]['WORKFLOW_ID'] and r['JOB_ID']] for n in NAMES},
                  capturedAt=datetime.now(timezone.utc).isoformat(), scope='WF1–WF4 only; existing catalog, schedules and WF5–WF10 untouched')
    return body, source
