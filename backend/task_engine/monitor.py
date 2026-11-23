"""PostgreSQL projections for the maintained Monitor, History and Log views."""
from datetime import datetime,timedelta,timezone
import os
import uuid

from .catalog import CatalogConfiguration,local_catalog
from .store import TERMINAL

ACTIVE={'QUEUED','RUNNING','UNKNOWN'}


def now():return datetime.now(timezone.utc)


def execution_scope(definition,identity):
    raw=definition['catalog'];by_id={w['WORKFLOW_ID']:w for w in raw['workflows']}
    if identity not in by_id:raise LookupError('Workflow is not in the PostgreSQL catalog')
    selected={identity};changed=True
    while changed:
        changed=False
        for rule in raw['raw_records']:
            a,b=rule.get('WORKFLOW_ID'),rule.get('PARENT_ID')
            if not rule.get('JOB_ID') and a in by_id and b in by_id and (a in selected or b in selected):
                before=len(selected);selected.update((a,b));changed|=len(selected)!=before
    issues=[by_id[i]['WORKFLOW_NAME']+': '+reason for i in sorted(selected) for reason in definition['executionIssues'].get(i,[])]
    parents={r['WORKFLOW_ID'] for r in raw['raw_records'] if not r.get('JOB_ID') and r.get('PARENT_ID')}
    roots=[by_id[i]['WORKFLOW_NAME'] for i in sorted(selected) if i not in parents]
    selected_keys={definition['workflowKeys'][i] for i in selected}
    return dict(keys=sorted(selected_keys),identities=sorted(selected),issues=issues,
        workflows=[n['name'] for n in definition['nodes'] if n['key'] in selected_keys],roots=roots,isRoot=identity not in parents)


def task_state(value):
    return 'QUEUED' if value in ('CREATING','SUBMITTING','SUBMITTED','SCHEDULED') else value


class Monitor:
    def __init__(self,store):self.store=store

    def records(self,conn,filters=None,latest=False,started=False,node_id=None,workflow_id=None):
        clauses=[];params=[];filters=filters or {}
        if node_id:clauses.append('n.id=%s');params.append(uuid.UUID(str(node_id)))
        if workflow_id or filters.get('workflowId'):
            clauses.append("COALESCE(c.snapshot->'workflowIds'->>n.node_key,g.source->'workflows'->>n.node_key,n.node_key)=%s")
            params.append(workflow_id or filters['workflowId'])
        if started:clauses.append('r.started_at IS NOT NULL')
        if filters.get('start'):
            clauses.extend(['COALESCE(r.started_at,c.created_at)<= %s',"COALESCE(r.ended_at,CASE WHEN c.state IN ('QUEUED','RUNNING','UNKNOWN') THEN clock_timestamp() ELSE c.ended_at END)>= %s"])
            params.extend((datetime.fromisoformat(filters['end']),datetime.fromisoformat(filters['start'])))
        elif filters.get('days'):
            clauses.append("c.created_at>=clock_timestamp()-(%s*interval '1 day')");params.append(filters['days'])
        if filters.get('cursor'):
            from orchestration_history import decode_cursor
            stamp,identity=decode_cursor(filters['cursor'])
            clauses.append('(c.created_at,n.id)<(%s,%s)');params.extend((stamp.replace(tzinfo=timezone.utc),uuid.UUID(identity)))
        if filters.get('status'):
            if filters.get('jobId'):
                clauses.append('EXISTS(SELECT 1 FROM kumo_task_engine.attempts a WHERE a.run_id=n.run_id AND a.job_key=%s AND a.state=%s)');params.extend((filters['jobId'],filters['status']))
            else:
                clauses.append("CASE WHEN r.state='RUNNING' AND r.started_at IS NULL THEN 'QUEUED' ELSE COALESCE(r.state,n.state) END=%s");params.append(filters['status'])
        if filters.get('jobId'):
            clauses.append("EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot_node.value->'plan'->'jobs') j WHERE j->>'key'=%s)");params.append(filters['jobId'])
        if filters.get('search'):
            clauses.append("(snapshot_node.value->>'name' ILIKE %s OR c.requested_by ILIKE %s OR n.id::text ILIKE %s OR EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot_node.value->'plan'->'jobs') j WHERE j->>'name' ILIKE %s))")
            params.extend(['%'+filters['search']+'%']*4)
        sql='''SELECT n.*,c.snapshot,c.graph_revision,c.request_id,c.requested_by,c.created_at AS requested_at,
            c.ended_at AS cascade_ended_at,c.state AS cascade_state,c.cancel_requested,r.state AS actual_state,r.started_at,r.ended_at,
            g.source,snapshot_node.value AS definition,
            COALESCE(c.snapshot->'workflowIds'->>n.node_key,g.source->'workflows'->>n.node_key,n.node_key) AS workflow_id
            FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.cascades c ON c.id=n.cascade_id
            JOIN kumo_task_engine.dependency_graphs g ON g.id=c.graph_id
            LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id
            JOIN LATERAL jsonb_array_elements(c.snapshot->'nodes') AS snapshot_node(value) ON snapshot_node.value->>'key'=n.node_key'''
        if clauses:sql+=' WHERE '+' AND '.join(clauses)
        if latest or started:
            sort="started_at DESC,id DESC" if started else "(cascade_state IN ('QUEUED','RUNNING','UNKNOWN')) DESC,requested_at DESC,id DESC"
            sql='SELECT * FROM (SELECT v.*,row_number() OVER(PARTITION BY workflow_id ORDER BY '+sort+') AS rank FROM ('+sql+') v) ranked WHERE rank=1'
        else:sql+=' ORDER BY c.created_at DESC,n.id DESC LIMIT '+str(int(filters.get('limit',100))+1)
        rows=conn.execute(sql,params).fetchall()
        ids=[r['run_id'] for r in rows if r['run_id']]
        attempts=conn.execute('SELECT * FROM kumo_task_engine.attempts WHERE run_id=ANY(%s) ORDER BY position',(ids,)).fetchall() if ids else []
        siblings=conn.execute('SELECT id,cascade_id,node_key FROM kumo_task_engine.cascade_nodes WHERE cascade_id=ANY(%s)',(list({r['cascade_id'] for r in rows}),)).fetchall() if rows else []
        for row in rows:
            row['attempts']=[a for a in attempts if a['run_id']==row['run_id']]
            row['siblings']={s['node_key']:str(s['id']) for s in siblings if s['cascade_id']==row['cascade_id']}
        return rows

    def run(self,row):
        definition=row['definition'];status=row['actual_state'] or row['state']
        if status=='ADMITTED':status='QUEUED'
        if status=='RUNNING' and not row['started_at']:status='QUEUED'
        root_id=str(row['id']);attempts={a['job_key']:a for a in row['attempts']}
        names={n['key']:n['name'] for n in row['snapshot']['nodes']}
        message='; '.join((names.get(b.get('parent'),b.get('parent',''))+': ' if b.get('parent') else '')+b.get('reason','') for b in row['blockers'])
        if row['cancel_requested'] and row['cascade_state'] in ACTIVE:message='Cancellation requested; awaiting task confirmation'
        root=dict(runId=root_id,executionId=root_id,workflowId=row['workflow_id'],workflowName=definition['name'],workflowGroup=definition.get('group',''),
            status=status,start=row['started_at'],end=row['ended_at'] or (row['cascade_ended_at'] if status in TERMINAL|{'BLOCKED'} else None),requestedAt=row['requested_at'],
            requestedBy=row['requested_by'],requestId=row['request_id'],mode='WORKFLOW',triggerSource='POSTGRES',source='POSTGRES',message=message,
            jobExecutionMode=definition['plan']['mode'],cascadeId=str(row['cascade_id']),graphRevision=row['graph_revision'],
            parentExecutionIds=[row['siblings'][p['key']] for p in definition['parents'] if p['key'] in row['siblings']],
            parentExecutionId=next((row['siblings'][p['key']] for p in definition['parents'] if p['key'] in row['siblings']),None),jobs=[])
        for index,job in enumerate(definition['plan']['jobs']):
            a=attempts.get(job['key']);enabled=job.get('enabled',True)
            state=task_state(a['state']) if a else 'SKIPPED' if not enabled or status in TERMINAL|{'BLOCKED'} else 'PENDING'
            previous=definition['plan']['jobs'][index-1]['key'] if index and definition['plan']['mode']=='SEQUENTIAL' else None
            parent=attempts.get(previous)
            root['jobs'].append(dict(runId=root_id,executionId=str(a['id']) if a else str(uuid.uuid5(row['id'],job['key'])),jobId=job['key'],
                name=job.get('name',job['key']),type='SQL',layer=job.get('layer','GENERAL'),position=index+1,status=state,selected=enabled,
                start=a['native_started_at'] if a else None,end=a['ended_at'] if a else None,requestedAt=row['requested_at'],
                message=(a['error'] or a['cleanup_error'] or '') if a else 'Disabled in this execution' if not enabled else message,
                queryId=a['query_id'] if a else None,cleanupState=a['cleanup_state'] if a else 'NONE',
                parentExecutionId=str(parent['id']) if parent else root_id,source='POSTGRES'))
        return root

    def payload(self):
        configuration=CatalogConfiguration(self.store)
        with self.store.connect() as conn:
            graph=configuration.graph(conn);catalog=configuration.payload(conn,graph)
            records=self.records(conn,latest=True);last=self.records(conn,started=True)
            workers=conn.execute('SELECT *,EXTRACT(EPOCH FROM clock_timestamp()-last_seen) AS age FROM kumo_task_engine.dependency_workers ORDER BY last_seen DESC LIMIT 1').fetchone()
        current={r['workflow_id']:self.run(r) for r in records};previous={r['workflow_id']:self.run(r) for r in last}
        active={r['workflow_id']:str(r['cascade_id']) for r in records if r['cascade_state'] in ACTIVE}
        assignments=catalog['assignments'];rows=[]
        for wf in catalog['workflows']:
            identity=wf['WORKFLOW_ID'];scope=execution_scope(graph['definition'],identity);run=current.get(identity)
            root=run or dict(status='NOT RUN',runId=None,executionId=None,start=None,end=None,requestedAt=None,message='',jobs=[])
            message='; '.join(scope['issues']) if scope['issues'] else 'Start the complete dependency group from '+', '.join(scope['roots']) if not scope['isRoot'] else 'Execution is already active or needs review' if any(i in active for i in scope['identities']) else 'Workflow is disabled' if not wf['IS_ENABLED'] else ''
            if not wf.get('ALLOW_MANUAL_TRIGGER',True):message='Manual execution is disabled'
            jobs=[];saved={j['jobId']:j for j in root['jobs']}
            members=[a for a in assignments if a['WORKFLOW_ID']==identity]
            for job in members:
                state=saved.get(job['JOB_ID']) or dict(status='NOT SELECTED' if run else 'NOT RUN',runId=root['runId'],start=None,end=None,message='',selected=False)
                old=next((j for j in previous.get(identity,{}).get('jobs',[]) if j['jobId']==job['JOB_ID']),None)
                jobs.append(dict(job,run=state,lastRun=old,progress=None,runOnly=False,hasSqlCondition=bool(job.get('SQL_CONDITION'))))
            for job in root['jobs']:
                if not any(j['JOB_ID']==job['jobId'] for j in jobs):jobs.append(dict(JOB_ID=job['jobId'],JOB_NAME=job['name'],JOB_TYPE=job['type'],JOB_LAYER=job['layer'],JOB_IS_ENABLED=False,run=job,lastRun=job,progress=None,runOnly=True))
            counts={s:sum(j['status']==s for j in root['jobs']) for s in ('SUCCESS','FAILED','SKIPPED','CANCELLED')};completed=sum(counts.values());total=len(root['jobs'])
            rows.append(dict(wf,source='POSTGRES',run=root,lastRun=previous.get(identity),jobs=jobs,canRun=not message,runUnavailable=message,
                runScope=scope['workflows'],graphId=str(graph['id']),configurationRevision=graph['revision'],cascadeId=root.get('cascadeId'),
                canCancel=identity in active,cancelRequested=next((r['cancel_requested'] for r in records if r['workflow_id']==identity),False),
                progress=dict(total=total,succeeded=counts['SUCCESS'],failed=counts['FAILED'],skipped=counts['SKIPPED'],cancelled=counts['CANCELLED'],completed=completed,percent=round(100*completed/total) if total else 0),
                queuedCount=0,scheduler=dict(state='NOT ENABLED',nextRun=None)))
        assigned={a['JOB_ID'] for a in assignments};unassigned=[j for j in catalog['jobs'] if j['JOB_ID'] not in assigned]
        if unassigned:
            rows.append(dict(source='POSTGRES',isUnassigned=True,WORKFLOW_ID='unassigned',WORKFLOW_NAME='Unassigned jobs',WORKFLOW_GROUP='Unassigned jobs',IS_ENABLED=False,IS_SCHEDULE_ENABLED=False,
                JOB_EXECUTION_MODE='SEQUENTIAL',run=dict(status='NOT RUN',runId=None,start=None,end=None,message='Assign these jobs to a workflow before execution'),lastRun=None,
                jobs=[dict(j,JOB_IS_ENABLED=j['IS_ENABLED'],JOB_SEQUENCE=i+1,run=dict(status='NOT RUN',runId=None,start=None,end=None,message='No workflow assignment'),lastRun=None,progress=None,runOnly=True) for i,j in enumerate(unassigned)],
                canRun=False,runUnavailable='These jobs have no workflow assignment',canCancel=False,progress=dict(total=0,succeeded=0,failed=0,skipped=0,percent=0),queuedCount=0))
        health='HEALTHY' if workers and workers['age']<=10 and workers['execution_enabled'] else 'DELAYED' if workers and workers['age']<=30 else 'UNAVAILABLE'
        return dict(ok=True,source='POSTGRES',rows=rows,workflowsCount=len(catalog['workflows']),jobsCount=len(catalog['jobs']),unassignedJobs=len(unassigned),
            generatedAt=now(),configurationRevision=graph['revision'],refreshSeconds=2,runsEnabled=os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()=='true',warnings=catalog['warnings'],submissions=[],
            worker=dict(state=health,lastSeen=workers['last_seen'] if workers else None),scheduleMessage='Imported schedules are not enabled in the PostgreSQL engine')

    def history(self,args):
        from orchestration_history import filters,encode_cursor
        values=filters(args)
        with self.store.connect() as conn:rows=self.records(conn,values)
        more=len(rows)>values['limit'];rows=rows[:values['limit']]
        return dict(ok=True,source='POSTGRES',runs=[self.run(r) for r in rows],generatedAt=now(),
            nextCursor=encode_cursor(dict(REQUESTED_AT=rows[-1]['requested_at'],EXECUTION_ID=str(rows[-1]['id']))) if more else None)

    def log(self,workflow_id,node_id,job_id=None):
        with self.store.connect() as conn:
            rows=self.records(conn,node_id=node_id,workflow_id=workflow_id)
            if not rows:raise LookupError('Execution does not belong to this workflow')
            row=rows[0];root=self.run(row)
            if job_id and not any(j['jobId']==job_id for j in root['jobs']):raise LookupError('Job is not in this execution snapshot')
            events=conn.execute('SELECT * FROM kumo_task_engine.events WHERE run_id=%s ORDER BY sequence',(row['run_id'],)).fetchall() if row['run_id'] else []
        jobs=[j for j in root['jobs'] if not job_id or j['jobId']==job_id]
        return dict(ok=True,source='POSTGRES',history=dict(WORKFLOW_NAME=root['workflowName'],WORKFLOW_ID=workflow_id,WORKFLOW_TYPE='SQL',STATUS=root['status'],
            REQUESTED_BY=root['requestedBy'],REQUESTED_AT=root['requestedAt'],START_TIME=root['start'],END_TIME=root['end'],TRIGGER_SOURCE='POSTGRES',ERROR_MESSAGE=root['message']),
            jobExecutions=[dict(JOB_POSITION=j['position'],JOB_NAME=j['name'],STATUS=j['status'],STARTED_AT=j['start'],ENDED_AT=j['end'],ERROR_MESSAGE=j['message'],QUERY_ID=j['queryId'],TASK_CLEANUP=j['cleanupState']) for j in jobs],
            runLog=[dict(LOG_DTTM=e['created_at'],ORIGIN='PostgreSQL worker',TYPE=e['kind'],MESSAGE=e['facts']) for e in events],modelProgress=[],testProgress=[],warnings={})
